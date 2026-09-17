#!/usr/bin/env python3
"""
Tahor decision queue: a tiny web app so pending classification decisions
(new vendor mappings, ambiguous keep/trash calls, free-text rule requests,
unsubscribe candidates, drafted replies) can be resolved from a browser
instead of a live session.

Data flow: the sweep scripts write rows into decisions.db when they hit
something they can't decide alone -> this app lets you resolve them ->
apply_decisions.py (a separate script, run by cron, or called inline from
/add-rule for immediate feedback) reads resolved rows and either applies
them directly (vendor mappings, sender rules: pure data/enforcement) or
hands free-text rules to a model to draft the change.

Google OAuth restricts access to the configured mailbox owner.
"""
import json
import hashlib
import fcntl
import os
import re
from html import escape
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, g, redirect, request, session, abort, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apply_decisions
import generate_sieve
import runtime_status
import config
import reply_rules
import provider_bridge
import mailbox_settings
import message_preview
import subscription_bulk_ui
import settings_autosave
import vendor_review_state
import decision_interactions
import tahor_db

DB_PATH = tahor_db.DB_PATH
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
SIEVE_PATH = DATA_DIR / "sieve.txt"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8420")

ALLOWED_EMAIL = os.environ.get("ALLOWED_EMAIL")
if not ALLOWED_EMAIL:
    raise SystemExit("Set ALLOWED_EMAIL to the one address allowed to sign in.")
ALLOWED_EMAIL = ALLOWED_EMAIL.lower()
REDIRECT_URI = f"{BASE_URL}/auth/google/callback"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Secret key for signed session cookies. Persisted to a local file so
# restarting the app doesn't invalidate every open session.
SECRET_KEY_PATH = Path(os.environ.get("TAHOR_SECRET_KEY_PATH", DB_PATH.parent / ".flask_secret_key"))
SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
with SECRET_KEY_PATH.with_suffix(".lock").open("a") as secret_lock:
    fcntl.flock(secret_lock, fcntl.LOCK_EX)
    if not SECRET_KEY_PATH.exists():
        from data_changes import atomic_write
        atomic_write(SECRET_KEY_PATH, secrets.token_hex(32))
    SECRET_KEY_PATH.chmod(0o600)

app = Flask(__name__)
app.secret_key = SECRET_KEY_PATH.read_text().strip()
app.config.update(SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=64 * 1024)


def html(value):
    return escape(str(value if value is not None else ""), quote=True)


@app.before_request
def protect_forms():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token", "")
        if not expected or not supplied.isascii() or not secrets.compare_digest(expected, supplied):
            abort(400, "This form expired. Reload the page and try again.")


@app.after_request
def secure_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"
    if response.mimetype == "text/html" and not response.is_streamed:
        body = response.get_data(as_text=True)
        if '<form ' in body:
            token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
            field = f'<input type="hidden" name="csrf_token" value="{html(token)}">'
            body = re.sub(r'(<form\b[^>]*method="post"[^>]*>)', lambda match: match[0] + field, body, flags=re.I)
            response.set_data(body)
        if '</body>' in body and 'data-email-preview' not in body:
            response.set_data(body.replace('</body>', message_preview.SCRIPT + '</body>'))
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("email") != ALLOWED_EMAIL:
            return redirect("/login")
        return view(*args, **kwargs)
    return wrapped


@app.route("/login")
def login():
    if not GOOGLE_CLIENT_ID:
        return "GOOGLE_CLIENT_ID not configured.", 500
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@app.route("/auth/google/callback")
def google_callback():
    expected_state = session.pop("oauth_state", None)
    supplied_state = request.args.get("state", "")
    if not expected_state or not supplied_state.isascii() or not secrets.compare_digest(supplied_state, expected_state):
        return "Invalid state.", 400
    code = request.args.get("code")
    if not code:
        return "Missing code.", 400

    token_resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]

    userinfo_resp = requests.get(
        GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
    )
    userinfo_resp.raise_for_status()
    userinfo = userinfo_resp.json()
    email = (userinfo.get("email") or "").lower()

    if email != ALLOWED_EMAIL or not userinfo.get("email_verified"):
        return "Access denied.", 403

    session.clear()
    session["email"] = email
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    tahor_db.init_db()


TAHOR_ICON = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%230B2624'/%3E%3Cpath fill-rule='evenodd' fill='%230EA5A0' d='M50,14 C50,14 22,56 22,68 A28,28 0 1 0 78,68 C78,56 50,14 50,14 Z M33,53 L50,65 L67,53 L67,59 L50,71 L33,59 Z'/%3E%3C/svg%3E"

TAHOR_HEADER = """
<header>
  <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path fill-rule="evenodd" fill="currentColor" d="M50,8 C50,8 18,54 18,68 A32,32 0 1 0 82,68 C82,54 50,8 50,8 Z M30,52 L50,66 L70,52 L70,59 L50,73 L30,59 Z"/></svg>
  <div class="wordmark-block">
    <div class="wordmark-row">
      <span class="wordmark">Tahor</span>
      <span class="hebrew" lang="he">טָהוֹר</span>
    </div>
    <div class="gloss">clean, pure</div>
  </div>
  <nav class="nav-links">{nav_links}</nav>
</header>
"""


def tahor_header(current):
    """current: the page you're on, rendered as plain (non-clickable) text so
    the nav's item order and position never shift between pages. Keys:
    'decisions', 'unsubscribe', 'drafts', 'settings'."""
    links = [
        ("decisions", "/", "Pending decisions"),
        ("unsubscribe", "/unsubscribe", "Unsubscribe"),
        ("settings", "/settings", "Settings"),
        ("status", "/status", "Status"),
    ]
    parts = []
    for key, href, label in links:
        if key == current:
            parts.append(f'<span class="nav-link nav-current">{label}</span>')
        else:
            parts.append(f'<a class="nav-link" href="{href}">{label}</a>')
    return TAHOR_HEADER.format(nav_links="".join(parts))

# Shared <style> block for every page in this app -- kept as one constant so
# the settings page matches the decision-queue page's look exactly instead of
# drifting from a copy-pasted stylesheet.
STYLE_BLOCK = """
<style>
  a { color: var(--accent); }
  :root {
    color-scheme: dark;
    --ground: #0B1F1D;
    --raised: #10302C;
    --well: #071716;
    --rule: #1C3B37;
    --accent: #2BC7BB;
    --accent-ink: #052220;
    --ink: #E2EEEC;
    --muted: #8FB1AC;
    --faint: #5C7F7A;
    --trash: #E0A39C;
    --display: "Fraunces", Georgia, serif;
    --text: "Source Sans 3", "Helvetica Neue", Arial, sans-serif;
    --mono: "DM Mono", ui-monospace, Menlo, monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      color-scheme: light;
      --ground: #F1F6F5;
      --raised: #E4EEEC;
      --well: #FFFFFF;
      --rule: #CBDEDB;
      --accent: #0B9C93;
      --accent-ink: #F1F6F5;
      --ink: #0B2624;
      --muted: #4B6E6A;
      --faint: #7C9A96;
      --trash: #A8463E;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 36px 20px 72px;
    background: var(--ground);
    color: var(--ink);
    font-family: var(--text);
    font-size: 16px;
    line-height: 1.5;
  }
  main { max-width: 640px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 12px; margin-bottom: 40px; }
  header svg { width: 30px; height: 30px; color: var(--accent); flex: none; }
  .wordmark-block { display: flex; flex-direction: column; gap: 2px; }
  .wordmark-row { display: flex; align-items: baseline; }
  .wordmark { font-family: var(--display); font-size: 1.7rem; font-weight: 500; line-height: 1; letter-spacing: -0.01em; }
  .hebrew { color: var(--muted); font-size: 1.05rem; margin-left: 10px; font-family: var(--text); }
  .gloss { color: var(--faint); font-size: 0.78rem; letter-spacing: 0.02em; }
  h1, h2 { font-family: var(--display); font-weight: 500; letter-spacing: -0.01em; margin: 0 0 16px; }
  h1 { font-size: 1.5rem; display: flex; align-items: baseline; gap: 10px; }
  h2 { font-size: 1.25rem; }
  .count { font-family: var(--text); font-size: 0.85rem; font-weight: 600; color: var(--accent); background: var(--raised); border: 1px solid var(--rule); border-radius: 999px; padding: 1px 10px; }
  section { margin-top: 44px; padding-top: 28px; border-top: 1px solid var(--rule); }
  .card { background: var(--raised); border: 1px solid var(--rule); border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; }
  .card.sieve { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 9%, var(--raised)); }
  .card.sieve .summary { color: var(--accent); }
  .card.warn { border-color: var(--trash); background: color-mix(in srgb, var(--trash) 9%, var(--raised)); }
  .card.warn .summary { color: var(--trash); }
  .card p { margin: 0 0 12px; }
  .summary { font-weight: 600; font-size: 1.05rem; margin-bottom: 6px; }
  .context { color: var(--muted); font-family: var(--mono); font-size: 0.82rem; line-height: 1.55; margin-bottom: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .fields { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }
  .fields select { grid-column: 1 / -1; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .fields label { display: grid; gap: 8px; }
  select, input[type=text], input[type=number], textarea {
    width: 100%;
    background: var(--well);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 6px;
    padding: 8px 10px;
    font: inherit;
    font-size: 0.95rem;
  }
  textarea { resize: vertical; margin-bottom: 12px; }
  ::placeholder { color: var(--faint); }
  select:focus, input:focus, textarea:focus, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button {
    background: var(--well);
    color: var(--ink);
    border: 1px solid var(--muted);
    border-radius: 6px;
    padding: 8px 16px;
    font: inherit;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
  }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  button.primary:hover { filter: brightness(1.08); }
  button.trash { color: var(--trash); border-color: var(--trash); }
  button.trash:hover { filter: brightness(1.15); }
  .empty { color: var(--muted); font-style: italic; margin: 0; }
  .hint { color: var(--muted); font-size: 0.92rem; margin: 0 0 12px; }
  pre {
    background: var(--well);
    border: 1px solid var(--rule);
    border-radius: 8px;
    padding: 14px 16px;
    margin: 0;
    font-family: var(--mono);
    font-size: 0.8rem;
    line-height: 1.55;
    color: var(--muted);
    overflow-x: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .nav-links { margin-left: auto; display: flex; gap: 18px; flex-wrap: wrap; }
  .nav-link { color: var(--muted); font-size: 0.85rem; text-decoration: none; border-bottom: 1px solid transparent; white-space: nowrap; }
  .nav-link:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .nav-current { color: var(--ink); font-weight: 600; cursor: default; }
  .mode-options { display: flex; flex-direction: column; gap: 12px; margin-bottom: 20px; }
  .mode-option { display: block; cursor: pointer; }
  .mode-option-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .mode-option-head input[type=radio] { accent-color: var(--accent); width: 16px; height: 16px; flex: none; }
  .mode-option.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 9%, var(--raised)); }
  .mode-option.active .summary { color: var(--accent); }
  .mode-option .context { margin-bottom: 0; }
  @media (max-width: 480px) {
    body { padding-top: 24px; }
    header { margin-bottom: 28px; }
    .fields { grid-template-columns: 1fr; }
    .actions button { flex: 1 1 auto; }
  }
</style>
"""

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tahor — pending decisions</title>
<link rel="icon" type="image/svg+xml" href="{icon}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Source+Sans+3:wght@400;600&family=DM+Mono&display=swap">
{style}
</head>
<body>
<main>
{header}
<p class="hint">{worker_summary} <a href="/status">Worker status</a></p>
<h1>Pending decisions <span class="count">{count}</span></h1>
{flash}
{sieve_banner}
{cards}
<section>
<h2>Add a free-text rule</h2>
<form method="post" action="/add-rule">
  <textarea name="rule_text" rows="3" placeholder="e.g. Trash marketing from offers.example, but keep its purchase receipts"></textarea>
  <button type="submit" class="primary">Submit rule to draft</button>
</form>
</section>
<section>
<h2>Current recommended Sieve filter</h2>
<p class="hint">Paste this into Fastmail: Settings &rarr; Filters &amp; Rules &rarr; Edit custom Sieve code (third box).</p>
<pre>{sieve_content}</pre>
<form method="post" action="/refresh-sieve"><button type="submit">Refresh proposal</button></form>
</section>
</main>
</body>
</html>
"""

SETTINGS_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tahor — Settings</title>
<link rel="icon" type="image/svg+xml" href="{icon}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Source+Sans+3:wght@400;600&family=DM+Mono&display=swap">
{style}
</head>
<body>
<main>
{header}
<h1>Settings</h1>
<h2>Mail classification</h2>
{status_line}
{classification_ai_settings}
<section>
<h2>Time in the inbox</h2>
<p class="hint">Keep read and unread messages in the inbox before filing them into folders. These delays run from delivery and do not delay classification, trash deletion, or retention cleanup.</p>
<form method="post" action="/settings" data-autosave>
  <input type="hidden" name="inbox_grace" value="1">
  <div class="fields">
    <label>Read mail (days)<input type="number" name="inbox_read_days" min="0" max="3650" value="{inbox_read_days}" required></label>
    <label>Unread mail (days)<input type="number" name="inbox_unread_days" min="0" max="3650" value="{inbox_unread_days}" required></label>
  </div>
  {autosave_status}
</form>
</section>
<section>
<h2>Automatic provider rules</h2>
<p class="hint">Optional high trust access: install whole-domain blocks in Fastmail before delivery. Marketing-only blocks stay in Tahor. Your administrator must enroll the isolated connector first.</p>
<p>{provider_status}</p>
<form method="post" action="/provider-sync">
  <button name="enabled" value="{provider_next_value}" type="submit"{provider_disabled}>{provider_button}</button>
</form>
<p class="hint">Turning this off stops future synchronization; installed rules remain. Unblock domains while enabled to remove their Tahor rules. Sign-in credentials never enter this page.</p>
</section>
<section>
<h2>AI rule drafting</h2>
<p class="hint">Turn a free-text instruction into a proposed mailbox rule. Review the exact action or diff before applying it. Manually entered reply rules remain available when this AI task is disabled.</p>
{rule_ai_settings}
</section>
<section>
<h2>Subscription recommendations</h2>
<p class="hint">Choose a separate model and describe which subscriptions you value. Recommendations use recent message samples and your private preferences; you review and apply every choice.</p>
{subscription_ai_settings}
</section>
<section>
<h2 id="reply-rules">Reply rules</h2>
<p class="hint">Review, edit and send replies in your mail app. Tahor saves a threaded draft in your mailbox and leaves the original unread. Nothing is sent automatically. Rules only draft messages still within your read/unread inbox timing above; drafting does not extend that window.</p>
{reply_rules_list}
<details><summary>Add a reply rule</summary>
<form method="post" action="/reply-rules/save">
  <p><label>Rule name<br><input name="name" required maxlength="120" placeholder="Community updates"></label></p>
  <p><label>Match using<br><select name="match_type"><option value="natural_language">Natural-language description</option><option value="sender_email">Specific email address</option><option value="sender_domain">Sender domain</option></select></label></p>
  <p><label>Which messages?<br><textarea name="match" rows="3" required maxlength="3000" placeholder="Updates and personal messages from community volunteers; exclude generic advertising."></textarea></label></p>
  <p><label>Directions for the reply<br><textarea name="instructions" rows="4" required maxlength="6000" placeholder="Thank them for the update. Mention the most urgent request if present and wish them well. For personal questions, respond to the actual request instead."></textarea></label></p>
  <p><label>Optional filing folder after inbox timing<br><input name="filing_folder" maxlength="250" placeholder="Existing Projects folder (leave blank for normal filing)"></label></p>
  <p><label>Signature<br><textarea name="signature" rows="2" maxlength="300" placeholder="Regards,&#10;Your name"></textarea></label></p>
  <p><label>Maximum sentences (before signature)<br><select name="max_sentences"><option>3</option><option>2</option><option>1</option></select></label></p>
  <button type="submit" class="primary">Save reply rule</button>
</form>
</details>
<h3>Reply writing and verification</h3>
<p class="hint">One policy controls both the writer and verifier for each attempt. Drafts stay in your mailbox for your review; nothing sends automatically.</p>
{reply_ai_settings}
</section>
</main>
{autosave_script}
<noscript>Enable JavaScript to save Settings changes automatically.</noscript>
</body>
</html>
"""

REPLY_TRIGGER_ROW = """
<div class="card">
  <div class="summary">{value}</div>
  <div class="context">{type_label}</div>
  <form method="post" action="/remove-reply-trigger">
    <input type="hidden" name="trigger_type" value="{type}">
    <input type="hidden" name="value" value="{value}">
    <button type="submit" class="trash">Remove</button>
  </form>
</div>
"""

MODE_OPTION = """
<label class="card mode-option{active_class}">
  <div class="mode-option-head">
    <input type="radio" name="{field}" value="{value}"{checked} onchange="this.form.submit()">
    <span class="summary">{label}</span>
    {active_badge}
  </div>
  <p class="context">{description}</p>
</label>
"""

FLASH_BANNER = """
<div class="card sieve">
  <div class="summary">{message}</div>
</div>
"""

SIEVE_BANNER = """
<div class="card sieve">
  <div class="summary">Sieve filter update recommended</div>
  <div class="context">{context}</div>
  <p>Paste the updated filter (below) into Fastmail's Sieve editor, then confirm:</p>
  <form method="post" action="/dismiss-sieve/{id}">
    <button type="submit" class="primary">I've applied this</button>
  </form>
</div>
"""

CARD_VENDOR_MAPPING = """
<div class="card">
  <div class="summary">{summary}</div>
  <div class="context">{context}</div>
  {sample_actions}
  <form method="post" action="/resolve/{id}">
    <div class="fields">
      <select name="bucket">
        <option value="">-- choose or type below --</option>
        {bucket_options}
      </select>
      <input type="text" name="bucket_custom" placeholder="or new bucket, e.g. Shopping/Retail">
      <input type="text" name="vendor_name" value="{suggested_vendor}" placeholder="Display name, e.g. Store name">
    </div>
    <div class="actions">
      <button type="submit" name="action" value="map" class="primary">Save routing rule</button>
      <button type="submit" name="action" value="skip">Leave unsorted</button>
    </div>
  </form>
</div>
"""

CARD_GENERIC = """
<div class="card">
  <div class="summary">{summary}</div>
  <div class="context">{context}</div>
  {details}
  <p><a href="/message/{id}">View email</a></p>
  <p class="context">Keep uses normal retention. Keep briefly deletes after the configured read/unread inbox window. Trash deletes permanently. Skip leaves this message protected and pending.</p>
  <form method="post" action="/resolve/{id}">
    <div class="actions">
      <button type="submit" name="action" value="keep" class="primary">Keep</button>
      <button type="submit" name="action" value="keep_brief">Keep briefly</button>
      <button type="submit" name="action" value="trash" class="trash">Trash</button>
      <button type="submit" name="action" value="skip">Skip for now</button>
    </div>
  </form>
  <form method="post" action="/message-details/{id}"><button type="submit">Refresh message details</button></form>
</div>
"""



UNSUBSCRIBE_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tahor — unsubscribe</title>
<link rel="icon" type="image/svg+xml" href="{icon}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Source+Sans+3:wght@400;600&family=DM+Mono&display=swap">
{style}
</head>
<body>
<main>
{header}
{flash}
<h1>Unsubscribe <span class="count">{count}</span></h1>
<p class="hint">Choose <strong>Stop marketing, keep transactions</strong> to request removal from this mailing list and have Tahor block future marketing while preserving receipts, payment notices, and other transactional messages. Unsubscribe alone requests removal without adding a block. The sender controls what its subscription covers; a confirmation page may require your attention. Block all mail also blocks transactional messages.</p>
{bulk_controls}
{non_compliant_banner}
{cards}
<section><h2>Blocked senders</h2>{blocked_senders}</section>
</main>
{interaction_script}
</body>
</html>
"""

NON_COMPLIANT_SECTION = """
<section>
<h2>Mail after an unsubscribe request</h2>
<p class="hint">Tahor classified these newer messages as marketing. Delivery can overlap with an unsubscribe request; review before blocking.</p>
{cards}
</section>
"""

UNSUBSCRIBE_CARD = """
<div class="card" id="subscription-{id}" data-subscription-id="{id}">
  <div class="summary">{display_name}</div>
  <p class="subscription-result" role="status" aria-live="polite">{result}</p>
  {manual_link}
  <div class="context">{sender_email} &middot; {message_count} message(s) &middot; {mechanism}</div>
  <fieldset><legend>Choose an action</legend>
    <label><input type="radio" name="choice-{id}" value="" checked> No action yet</label><br>
    <label><input type="radio" name="choice-{id}" value="unsubscribe_block_marketing"> Stop marketing, keep transactions</label><br>
    <label><input type="radio" name="choice-{id}" value="unsubscribe"> Unsubscribe only</label><br>
    <label><input type="radio" name="choice-{id}" value="dismiss"> Keep subscription</label><br>
    <label><input type="radio" name="choice-{id}" value="block_all"> Unsubscribe and block all mail, including receipts</label>
  </fieldset>
</div>
"""

NON_COMPLIANT_CARD = UNSUBSCRIBE_CARD.replace('class="card"', 'class="card warn"').replace('{mechanism}', 'marketing received after an unsubscribe request')


def decision_context(row):
    raw = row["context"] or ""
    try:
        context = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(context, dict):
        return str(context)
    if context.get("note") and row["kind"] != "message_review":
        return str(context["note"])
    if row["kind"] == "vendor_mapping":
        sender = context.get('sender_email') or context.get('sender_label', 'unknown')
        parts = [f'Sender: {sender}']
        if context.get('display_name'):
            parts.append('Name: ' + str(context['display_name']))
        samples = context.get('samples') or [context]
        for sample in samples[-3:] if isinstance(samples, list) else []:
            if isinstance(sample, dict) and sample.get('subject'):
                parts.append('Example: ' + str(sample['subject']) + (' · ' + str(sample.get('received_at') or sample.get('date')) if sample.get('received_at') or sample.get('date') else ''))
        if context.get('suggestion_source') == 'ai':
            parts.append('Tahor needs your review for this sender. Suggested action: ' + str(context.get('suggested_action', 'review')) + '. ' + str(context.get('suggestion_reason', ''))[:500])
        parts.append('This rule applies to this exact sender address.' if context.get('routing_key') else 'Sender details have not been captured yet. Confirm the merchant before saving a domain-wide rule.')
        return ' · '.join(parts)
    if row["kind"] == "message_review":
        parts = [f'From: {context.get("sender") or ("Not provided in this email" if context.get("details_loaded") else "Loading in the background")}']
        received = context.get('received_at') or context.get('date')
        if received:
            try:
                from email.utils import parsedate_to_datetime
                try:
                    instant = datetime.fromisoformat(received.replace('Z', '+00:00'))
                except ValueError:
                    instant = parsedate_to_datetime(received)
                if instant.tzinfo is None:
                    raise ValueError()
                instant = instant.astimezone(timezone.utc)
                seconds = max(0, (datetime.now(timezone.utc) - instant).total_seconds())
                age = f'{int(seconds // 86400)} days old' if seconds >= 86400 else (f'{int(seconds // 3600)} hours old' if seconds >= 3600 else 'less than an hour old')
                parts.append(('Received: ' if context.get('received_at') else 'Message date: ') + instant.strftime('%Y-%m-%d %H:%M UTC') + f' ({age})')
            except (ValueError, TypeError, AttributeError, OverflowError):
                parts.append('Date unavailable; refresh message details')
        else:
            parts.append('Date unavailable in this email' if context.get('details_loaded') else 'Date is loading in the background')
        parts.append(f'In {context.get("mailbox", "your mailbox")}. Protected from retention cleanup until you decide.')
        return ' · '.join(parts)
    return str(context.get("outcome") or context.get("explanation") or "Ready for your review.")


def known_buckets(db):
    import config
    buckets = set()
    try:
        saved_buckets = config.vendor_buckets()
    except FileNotFoundError:
        saved_buckets = {}
    for value in saved_buckets.values():
        if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], str) and value[0].strip():
            buckets.add(value[0].strip())
    for row in db.execute("SELECT resolution FROM decisions WHERE resolution IS NOT NULL"):
        try:
            value = json.loads(row["resolution"])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("bucket"), str):
            buckets.add(value["bucket"])
    return sorted(buckets)


def observed_rule_domains(db):
    """Only syntactically valid domains actually observed in this owner's mail."""
    choices = {}
    for row in db.execute('SELECT sender_domain,display_name FROM unsubscribe_candidates ORDER BY sender_domain'):
        try:
            generate_sieve.domain_test(row['sender_domain'])
        except (ValueError, TypeError):
            continue
        choices[row['sender_domain']] = row['display_name'] or row['sender_domain']
    return choices


@app.route("/")
@login_required
def index():
    db = get_db()
    pending = db.execute(
        "SELECT * FROM decisions WHERE status = 'pending' AND kind != 'sieve_update' ORDER BY created_at ASC"
    ).fetchall()

    buckets = known_buckets(db)
    bucket_options = "".join(f'<option value="{html(b)}">{html(b)}</option>' for b in buckets)

    import vendor_suggestions
    automatic_ids = set(vendor_suggestions.pending_work_ids(db)) if mailbox_settings.is_ai_enabled('rule') else set()
    automatic_count = 0
    cards = []
    for row in pending:
        ctx = decision_context(row)
        if row["kind"] == "free_text_rule":
            try:
                rule_context = json.loads(row['context'] or '{}')
                proposal = rule_context.get('rule_proposal')
                clarification = rule_context.get('rule_clarification')
            except (ValueError, TypeError, AttributeError):
                proposal = None
                clarification = None
            if clarification and not proposal:
                try:
                    original = json.loads(row["resolution"] or "{}").get("text", "")
                except (ValueError, TypeError, AttributeError):
                    original = ""
                domains = observed_rule_domains(db)
                options = ''.join(f'<option value="{html(domain)}">{html(label)}</option>' for domain, label in domains.items())
                chooser = (f'<label>Choose an observed sender domain (optional)<input type="text" name="observed_domain" list="rule-domains-{row["id"]}" placeholder="Search domains or sender names" autocomplete="off"></label><datalist id="rule-domains-{row["id"]}">{options}</datalist><p>These domains came from your mailbox. Selecting one explicitly adds that exact target to this instruction; no domain is selected automatically.</p>' if domains else '')
                cards.append(f'<div class="card"><div class="summary">Rule needs clarification</div><p>{html(clarification.get("question", "Please clarify your instruction."))}</p><form method="post" action="/clarify-rule/{row["id"]}"><input type="hidden" name="revision" value="{rule_revision(row)}">{chooser}<label>Full instruction, including the exact sender domain when applicable<textarea name="rule_text" rows="4" required>{html(original)}</textarea></label><p>Use a full domain such as alerts.example.com, not a brand name. Include what you want Tahor to do. The revised proposal still needs your approval.</p><button type="submit">Resubmit instruction</button></form></div>')
            elif proposal:
                result = proposal['result']
                if result.get('kind') == 'sender_rule':
                    sender = result.get('sender_rule') or {}
                    details = (f"Domain: {sender.get('domain', '')}\n"
                               f"Action: {sender.get('rule', '')}\n"
                               f"Attempt unsubscribe: {'Yes' if sender.get('attempt_unsubscribe') is True else 'No'}")
                else:
                    details = proposal['diff']
                cards.append(f'<div class="card"><div class="summary">{html(row["summary"])}</div><p>Review this model proposal before changing your rules.</p><pre style="white-space:pre-wrap">{html(details)}</pre><form method="post" action="/review-rule/{row["id"]}"><input type="hidden" name="proposal" value="{html(proposal["token"])}"><button name="action" value="approve">Approve these changes</button><button name="action" value="reject">Reject proposal</button></form></div>')
            else:
                cards.append(f'<div class="card"><div class="summary">{html(row["summary"])}</div><form method="post" action="/retry-rule/{row["id"]}"><button type="submit">Retry rule</button></form></div>')
        elif row["kind"] == "vendor_mapping":
            try:
                vendor_context = json.loads(row['context'] or '{}')
            except (ValueError, TypeError):
                vendor_context = {}
            if not isinstance(vendor_context, dict):
                vendor_context = {}
            if vendor_review_state.reconcile(db, row, vendor_context):
                continue
            if ('vendor:' + str(row['id']) in automatic_ids or (mailbox_settings.is_ai_enabled('rule') and vendor_context.get('automatic_vendor_mapping'))):
                automatic_count += 1
                continue
            suggested_bucket = vendor_context.get('suggested_bucket')
            vendor_buckets = set(buckets)
            if isinstance(suggested_bucket, str) and suggested_bucket.strip():
                vendor_buckets.add(suggested_bucket)
            vendor_options = ''.join(f'<option value="{html(bucket)}"' + (' selected' if bucket == suggested_bucket else '') + f'>{html(bucket)}</option>' for bucket in sorted(vendor_buckets))
            sample_actions = []
            for sample_index, sample in enumerate(vendor_context.get('samples', [])):
                if not isinstance(sample, dict) or not sample.get('mailbox') or not sample.get('message_id'):
                    continue
                completed = vendor_review_state.completed_action(db, sample)
                if completed in ('keep_brief', 'trash'):
                    continue
                if completed:
                    sample_actions.append(f'<p>This sample already has a completed message decision. <a href="/message/{row["id"]}/{sample_index}">View email</a></p>')
                    continue
                sample_actions.append(f'<div><a href="/message/{row["id"]}/{sample_index}">View email: {html(sample.get("subject") or "(No subject)")}</a><form method="post" action="/vendor-message/{row["id"]}/{sample_index}"><button name="action" value="keep_brief">Keep this email briefly</button><button name="action" value="trash" class="trash">Trash this email</button></form></div>')
            cards.append(
                CARD_VENDOR_MAPPING.format(
                    id=row["id"], summary=html(row["summary"]), context=html(ctx), bucket_options=vendor_options, suggested_vendor=html(vendor_context.get("suggested_vendor") or vendor_context.get("display_name") or ""), sample_actions="".join(sample_actions)
                )
            )
        else:
            try:
                details_context = json.loads(row['context'] or '{}')
            except (ValueError, TypeError):
                details_context = {}
            snippet = details_context.get('snippet') if isinstance(details_context, dict) else None
            details = ('<details><summary>Message excerpt</summary><p>' + html(snippet[:500]) + '</p></details>') if isinstance(snippet, str) and snippet.strip() else ''
            try:
                saved_action = json.loads(row['resolution'] or '{}').get('action')
            except (ValueError, TypeError, AttributeError):
                saved_action = None
            if saved_action in ('keep', 'keep_brief', 'trash'):
                label = {'keep': 'Keep', 'keep_brief': 'Keep briefly', 'trash': 'Trash'}[saved_action]
                ctx += f' Your choice ({label}) is saved and will retry automatically. Skip pauses this retry.'
            cards.append(CARD_GENERIC.format(id=row["id"], summary=html(row["summary"] or '(No subject)'), context=html(ctx), details=details))

        if cards:
            cards[-1] = cards[-1].replace('<div class="card"', f'<div class="card" data-decision-id="{row["id"]}" data-decision-kind="{row["kind"]}"', 1)

    review_count = len(cards)
    body = "".join(cards) if cards else '<p class="empty">No decisions need your attention.</p>'
    if automatic_count:
        body = (f'<div class="card"><div class="summary">Automatic filing is processing {automatic_count} sender(s)</div><p>Confident routine matches are filed automatically. Only uncertain cases need your review. Temporary failures retry automatically.</p></div>' + body)

    sieve_row = db.execute(
        "SELECT * FROM decisions WHERE kind = 'sieve_update' AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    sieve_banner = (
        SIEVE_BANNER.format(id=sieve_row["id"], context=html(sieve_row["context"]))
        if sieve_row else ""
    )
    provider = provider_bridge.status()
    if provider['enabled']:
        sieve_banner = '<div class="card sieve"><div class="summary">Automatic provider rules</div><p>' + html(provider['label']) + '</p><p>Whole-domain blocks synchronize through the isolated connector. Manage this under Settings.</p></div>'
    sieve_content = SIEVE_PATH.read_text() if SIEVE_PATH.exists() else "(not yet synced)"

    flash_message = session.pop("flash", None)
    flash = FLASH_BANNER.format(message=html(flash_message)) if flash_message else ""

    return PAGE_TEMPLATE.format(
        icon=TAHOR_ICON,
        style=STYLE_BLOCK,
        header=tahor_header("decisions"),
        worker_summary=html(runtime_status.describe_status()),
        count=review_count,
        flash=flash,
        sieve_banner=sieve_banner,
        cards=body,
        sieve_content=html(sieve_content),
    ) + decision_interactions.SCRIPT


def _settings_status_line(current_mode):
    backlog, _ = mailbox_settings.get_cached_backlog(mailbox_settings.BACKLOG_REFRESH_SECONDS)
    if backlog is None:
        return ''
    return (f'<p class="hint">Backlog estimate: approximately {int(backlog)} messages. '
            'The Status page shows actual processing and retries; queue completion estimates are not guarantees.</p>')


AI_POLICY_OPTIONS = (
    ('paid_only', 'Always paid', 'Use only the selected paid model. Provider failures stay pending and retry; never use a free model.'),
    ('paid', 'Paid with free fallback', 'Use paid normally. Use the free model only when the paid provider fails, then periodically retry paid.'),
    ('auto', 'Balanced', 'Start free. Temporarily use paid if free is unavailable or the estimated queue exceeds four hours; return to free afterward.'),
    ('free', 'Always free', 'Use only the selected free model. Errors stay pending and retry; never call a paid model.'),
)


def render_ai_task_settings(task):
    policy = mailbox_settings.get_ai_policy(task)
    models = mailbox_settings.get_ai_models(task)
    enabled = mailbox_settings.is_ai_enabled(task)
    cards = ''.join(
        f'<label class="card mode-option{" active" if key == policy else ""}">'
        f'<div class="mode-option-head"><input type="radio" name="ai_policy" value="{key}"{" checked" if key == policy else ""}>'
        f'<span class="summary">{label}</span></div><p class="context">{description}</p></label>'
        for key, label, description in AI_POLICY_OPTIONS)
    if task == 'classification':
        controls = '<p class="hint">Paid model: Gemini 3.8 Flash. Free model: Ling 3.0 Flash VL.</p>'
        caution = 'In a selected 24-message test, the best tested free classifier wrongly trashed five messages, including medical and family correspondence. This is a small test, not an accuracy guarantee; choose a policy that fits the risk of losing useful mail.'
        import classify
        if not classify.free_classification_enabled():
            controls += '<p class="hint">Free classification is disabled by the server configuration. Policies that require it cannot be selected.</p>'
    else:
        registry = mailbox_settings.ai_model_registry(task)
        def choices(tier):
            entries = [(key, value) for key, value in registry.items() if (key == 'none' and tier == 'paid') or (key != 'none' and value['model'].endswith(':free') == (tier == 'free'))]
            return ''.join(f'<option value="{html(key)}"{" selected" if key == models[tier] else ""}>{html(value["label"])}</option>' for key, value in entries)
        controls = (f'<p><label>Paid model <select name="paid_model">{choices("paid")}</select></label></p>'
                    f'<p><label>Free model <select name="free_model">{choices("free")}</select></label></p>'
                    '<p class="hint">Selecting Disabled for the paid model disables this writing task, regardless of policy. Choosing a paid model makes it available; Always free still never calls it.</p>')
        caution = ('Free reply drafts can contain unsupported promises, incorrect roles, or invented details even after model verification. Review every draft before sending.' if task == 'reply' else
                   'In an eight-instruction test, the free rule model proposed the wrong folder once. Review every proposed action and diff; model validation does not establish your intent.')
    if task == 'subscriptions':
        controls += (f'<p><label>Recommendations per batch <input type="number" name="batch_size" min="1" max="200" value="{mailbox_settings.get_subscription_batch_size()}" required></label></p>'
                     f'<p><label>Your subscription preferences<textarea name="subscription_guidance" rows="5" maxlength="12000">{html(mailbox_settings.load_settings().get("subscription_guidance", ""))}</textarea></label></p>')
        caution = 'Recommendations only preselect choices for your review. They never unsubscribe or block automatically. Missing history or unclear mail may lead to imperfect suggestions; review each batch before applying it.'
    status = 'Enabled' if enabled else 'Disabled'
    return (f'<form method="post" action="/settings" class="ai-task-settings" data-autosave data-ai-task="{task}">'
            f'<input type="hidden" name="ai_task" value="{task}"><p><strong class="ai-enabled-status">{status}</strong></p>'
            f'<div class="mode-options">{cards}</div>{controls}'
            f'<p class="hint">{caution}</p><p class="hint">All hosted routes require zero data retention and prohibit data collection. The free route is restricted to Novita; privacy routing does not guarantee answer quality.</p>'
            + settings_autosave.STATUS + '</form>')


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        if "ai_task" in request.form:
            task = request.form.get('ai_task', '')
            policy = request.form.get('ai_policy', '')
            try:
                import classify
                if task == 'classification' and policy in ('auto', 'free') and not classify.free_classification_enabled():
                    raise ValueError('Free classification is disabled by the server configuration.')
                mailbox_settings.set_ai_task_settings(task, policy, paid_model=request.form.get('paid_model'), free_model=request.form.get('free_model'), batch_size=request.form.get('batch_size') if task == 'subscriptions' else None, guidance=request.form.get('subscription_guidance') if task == 'subscriptions' else None)
            except ValueError as exc:
                if request.headers.get('Accept') == 'application/json':
                    return {'saved': False, 'message': str(exc)}, 400
                abort(400, str(exc))
        elif "inbox_grace" in request.form:
            try:
                mailbox_settings.set_inbox_grace_days(request.form.get("inbox_read_days", ""), request.form.get("inbox_unread_days", ""))
            except ValueError as exc:
                if request.headers.get('Accept') == 'application/json':
                    return {'saved': False, 'message': str(exc)}, 400
                abort(400, str(exc))
        elif "classify_mode" in request.form:
            mode = request.form.get("classify_mode", "")
            if mode in mailbox_settings.MODES:
                import classify
                if mode in ("free", "auto") and not classify.free_classification_enabled():
                    abort(400, "Free classification is disabled by the server configuration.")
                mailbox_settings.set_classify_mode(mode)
            else:
                abort(400, "Choose Free, Paid, or Auto.")
        elif "rule_model" in request.form:
            key = request.form.get("rule_model", "")
            if key in mailbox_settings.RULE_MODELS:
                mailbox_settings.set_rule_model(key)
            else:
                abort(400, "Choose an available rule model.")
        elif "reply_backup_model" in request.form:
            try:
                mailbox_settings.set_reply_backup_model(request.form.get("reply_backup_model", ""))
            except ValueError:
                abort(400, "Choose an explicitly free reply model.")
        elif "reply_model" in request.form:
            key = request.form.get("reply_model", "")
            if key in mailbox_settings.REPLY_MODELS:
                mailbox_settings.set_reply_model(key)
            else:
                abort(400, "Choose an available reply model.")
        else:
            abort(400, "No setting was selected.")
        if request.headers.get('Accept') == 'application/json':
            result = {'saved': True}
            if request.form.get('ai_task') in mailbox_settings.AI_TASKS:
                result['enabled'] = mailbox_settings.is_ai_enabled(request.form['ai_task'])
            return result
        return redirect("/settings")

    current_mode = mailbox_settings.get_classify_mode()
    provider = provider_bridge.status()
    return SETTINGS_PAGE_TEMPLATE.format(
        autosave_status=settings_autosave.STATUS,
        autosave_script=settings_autosave.SCRIPT,
        icon=TAHOR_ICON,
        style=STYLE_BLOCK,
        header=tahor_header("settings"),
        inbox_read_days=mailbox_settings.get_inbox_grace_days()["read"],
        inbox_unread_days=mailbox_settings.get_inbox_grace_days()["unread"],
        status_line=_settings_status_line(current_mode),
        classification_ai_settings=render_ai_task_settings('classification'),
        rule_ai_settings=render_ai_task_settings('rule'),
        subscription_ai_settings=render_ai_task_settings('subscriptions'),
        reply_ai_settings=render_ai_task_settings('reply'),
        reply_rules_list=render_reply_rules(),
        provider_status=html(provider['label']),
        provider_next_value='0' if provider['enabled'] else '1',
        provider_disabled=' disabled' if provider['state'] == 'not_configured' else '',
        provider_button='Turn off automatic rules' if provider['enabled'] else 'Enable automatic rules',
    )


def render_reply_rules():
    cards = []
    for rule in reply_rules.get_rules(False):
        identifier = html(rule['id'])
        excluded = set(rule.get('excluded_senders', []))
        senders = tahor_db.reply_rule_senders(rule['id'])
        sender_rows = []
        for row in senders:
            sender = row['sender']
            is_excluded = sender in excluded
            sender_rows.append(f'<form method="post" action="/reply-rules/exclude"><input type="hidden" name="rule_id" value="{identifier}"><input type="hidden" name="sender" value="{html(sender)}"><p>{html(sender)} — {row["messages"]} matched message(s) — {"opted out" if is_excluded else "drafting allowed"} <button name="excluded" value="{"0" if is_excluded else "1"}">{"Allow drafts" if is_excluded else "Opt out"}</button></p></form>')
        options = ''.join(f'<option value="{kind}"{" selected" if kind == rule["match_type"] else ""}>{label}</option>' for kind, label in [('natural_language','Natural-language description'),('sender_email','Specific email address'),('sender_domain','Sender domain')])
        sentence_options = ''.join(f'<option{" selected" if count == rule.get("max_sentences",3) else ""}>{count}</option>' for count in (3,2,1))
        cards.append(f'''<div class="card"><h3 data-rule-display="name">{html(rule['name'])}</h3><p>{"Enabled" if rule.get('enabled', True) else "Paused"}</p>
<p data-rule-display="match">{html(rule['match'])}</p><p data-rule-display="instructions">{html(rule['instructions'])}</p><pre data-rule-display="signature">{html(rule.get('signature',''))}</pre>
<details><summary>Matched senders ({len(senders)}) and opt-outs</summary><p class="hint">Opting out stops future drafts from that sender for this rule. Existing drafts stay in your mailbox; mail protection and normal inbox timing are unchanged.</p>{''.join(sender_rows) or '<p>No matched senders yet.</p>'}</details>
<details><summary>Edit rule</summary><form method="post" action="/reply-rules/save" data-autosave>
<input type="hidden" name="rule_id" value="{identifier}">
<p><label>Name<br><input name="name" value="{html(rule['name'])}" required maxlength="120"></label></p>
<p><label>Match using<br><select name="match_type">{options}</select></label></p>
<p><label>Which messages?<br><textarea name="match" rows="3" required maxlength="3000">{html(rule['match'])}</textarea></label></p>
<p><label>Reply directions<br><textarea name="instructions" rows="4" required maxlength="6000">{html(rule['instructions'])}</textarea></label></p>
<p><label>Filing folder after inbox timing<br><input name="filing_folder" maxlength="250" value="{html(rule.get('filing_folder',''))}"></label></p>
<p><label>Signature<br><textarea name="signature" rows="2" maxlength="300">{html(rule.get('signature',''))}</textarea></label></p>
<p><label>Maximum sentences<br><select name="max_sentences">{sentence_options}</select></label></p>
{settings_autosave.STATUS}</form></details>
<form method="post" action="/reply-rules/toggle"><input type="hidden" name="rule_id" value="{identifier}"><button name="enabled" value="{'0' if rule.get('enabled', True) else '1'}">{'Pause rule' if rule.get('enabled', True) else 'Enable rule'}</button></form></div>''')
    return ''.join(cards) or '<p>No reply rules yet. Add one below.</p>'


@app.route('/reply-rules/save', methods=['POST'])
@login_required
def save_reply_rule():
    try:
        rule_id = reply_rules.save_rule(request.form.get('name',''), request.form.get('match_type',''), request.form.get('match',''), request.form.get('instructions',''), request.form.get('signature',''), request.form.get('max_sentences','3'), request.form.get('rule_id') or None, filing_folder=request.form.get('filing_folder','').strip())
    except ValueError as error:
        if request.headers.get('Accept') == 'application/json':
            return {'saved': False, 'message': str(error)}, 400
        abort(400, str(error))
    if request.headers.get('Accept') == 'application/json':
        rule = next(rule for rule in reply_rules.get_rules(False) if rule['id'] == rule_id)
        return {'saved': True, 'rule': {key: rule.get(key, '') for key in ('name', 'match', 'instructions', 'signature')}}
    return redirect('/settings#reply-rules')


@app.route('/reply-rules/toggle', methods=['POST'])
@login_required
def toggle_reply_rule():
    if request.form.get('enabled') not in ('0','1'):
        abort(400, 'Choose enabled or paused.')
    try:
        reply_rules.set_enabled(request.form.get('rule_id',''), request.form['enabled'] == '1')
    except ValueError as error:
        abort(400, str(error))
    return redirect('/settings#reply-rules')


@app.route('/reply-rules/exclude', methods=['POST'])
@login_required
def exclude_reply_sender():
    if request.form.get('excluded') not in ('0','1'):
        abort(400, 'Choose whether to opt out.')
    try:
        reply_rules.set_sender_excluded(request.form.get('rule_id',''), request.form.get('sender',''), request.form['excluded'] == '1')
    except ValueError as error:
        abort(400, str(error))
    return redirect('/settings#reply-rules')


@app.route("/provider-sync", methods=["POST"])
@login_required
def provider_sync():
    enabled = request.form.get("enabled")
    if enabled not in ('0', '1'):
        return 'Invalid provider setting', 400
    try:
        provider_bridge.publish(enabled == '1')
    except (ValueError, OSError):
        return 'Connector unavailable. Ask the administrator to complete enrollment.', 503
    session['flash'] = 'Provider synchronization queued.' if enabled == '1' else 'Future provider synchronization stopped. Installed rules remain.'
    return redirect('/settings')


@app.route("/add-reply-trigger", methods=["POST"])
@login_required
def add_reply_trigger():
    trigger_type = request.form.get("trigger_type", "")
    value = request.form.get("value", "")
    if trigger_type in mailbox_settings.TRIGGER_TYPES and value.strip():
        try:
            mailbox_settings.add_reply_trigger(trigger_type, value)
        except ValueError as exc:
            abort(400, str(exc))
    else:
        abort(400, "Choose a trigger type and enter a sender.")
    return redirect("/settings")


@app.route("/remove-reply-trigger", methods=["POST"])
@login_required
def remove_reply_trigger():
    mailbox_settings.remove_reply_trigger(request.form.get("trigger_type", ""), request.form.get("value", ""))
    return redirect("/settings")


@app.route("/refresh-sieve", methods=["POST"])
@login_required
def refresh_sieve():
    try:
        changed = generate_sieve.refresh_sieve()
        session["flash"] = "Sieve proposal updated. Review it before installing." if changed else "Sieve proposal is current."
    except Exception as exc:
        session["flash"] = f"Could not refresh the Sieve proposal: {exc}. You can retry."
    return redirect("/")


@app.route("/dismiss-sieve/<int:decision_id>", methods=["POST"])
@login_required
def dismiss_sieve(decision_id):
    db = get_db()
    db.execute(
        "UPDATE decisions SET status = 'resolved', resolved_at = ? WHERE id = ? AND kind = 'sieve_update'",
        (datetime.now(timezone.utc).isoformat(), decision_id),
    )
    db.commit()
    return redirect("/")


def _review_context(decision_id, sample_index=None):
    row = get_db().execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        context = json.loads(row['context'] or '{}')
        if sample_index is not None:
            if row['kind'] != 'vendor_mapping':
                abort(400)
            context = context['samples'][sample_index]
        elif row['kind'] != 'message_review':
            abort(400)
        if not isinstance(context, dict) or not context.get('mailbox') or not context.get('message_id'):
            abort(409, 'Message details have not been captured yet. Let the filing scan refresh this sender.')
    except (ValueError, TypeError, KeyError, IndexError):
        abort(409, 'Message details have not been captured yet. Let the filing scan refresh this sender.')
    return row, context


@app.route('/message/<int:decision_id>')
@app.route('/message/<int:decision_id>/<int:sample_index>')
@login_required
def view_message(decision_id, sample_index=None):
    import message_reviews
    _, context = _review_context(decision_id, sample_index)
    try:
        details, body = message_reviews.read_message(context)
    except (ValueError, RuntimeError):
        return 'The message could not be read safely. Return to Pending decisions and refresh its details, or open it in your mail client.', 409
    except Exception:
        return 'The mailbox is temporarily unavailable. Your message remains unread and unchanged.', 503
    return ('<!doctype html><html><head><meta charset="utf-8"><title>Tahor — message</title>' + STYLE_BLOCK + '</head><body><main>' + tahor_header('') +
            '<p><a href="/">Back to pending decisions</a></p><h1>' + html(details.get('subject') or '(No subject)') +
            '</h1><p>From: ' + html(details.get('sender', '')) + '</p><p>Received: ' + html(details.get('received_at', '')) +
            '</p><p>This read-only text view does not mark the email read. Remote images and attachments are not displayed.</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + html(body) + '</pre></main></body></html>')


@app.route('/vendor-message/<int:decision_id>/<int:sample_index>', methods=['POST'])
@login_required
def review_vendor_message(decision_id, sample_index):
    row, context = _review_context(decision_id, sample_index)
    action = request.form.get('action')
    if action not in ('keep_brief', 'trash'):
        abort(400)
    db = get_db()
    review, identity = vendor_review_state.sample_review(db, context)
    if review is not None and identity.get('applied') is True:
        vendor_review_state.reconcile(db, row, json.loads(row['context']))
        session['flash'] = 'This message was already handled. Its completed choice has not been changed.'
        return redirect('/')
    if row['status'] != 'pending':
        session['flash'] = 'This sender decision was already handled. Refresh to see current work.'
        return redirect('/')
    if review is None:
        tahor_db.queue_message_review(context['mailbox'], context['message_id'], context.get('subject', ''), context.get('uid'), context.get('uidvalidity'), metadata=context)
        review, identity = vendor_review_state.sample_review(db, context)
    if review is None:
        abort(409, 'The message identity is ambiguous. No mailbox action was repeated.')
    if review['status'] != 'pending' or review['resolution'] is not None:
        session['flash'] = 'This message already has a saved choice being processed. No second action was started.'
        return redirect('/')
    review_context = dict(identity, vendor_source_identity={key: context.get(key) for key in ('mailbox', 'message_id', 'uid', 'uidvalidity')})
    with db:
        changed = db.execute("UPDATE decisions SET status='resolved',context=?,resolution=?,resolved_at=? WHERE id=? AND status='pending' AND context=? AND resolution IS ?",
            (json.dumps(review_context), json.dumps({'action': action}), datetime.now(timezone.utc).isoformat(), review['id'], review['context'], review['resolution']))
    if changed.rowcount != 1:
        session['flash'] = 'This message already has a saved choice being processed. No second action was started.'
        return redirect('/')
    try:
        session['flash'] = apply_decisions.apply_one(review['id'])
    except Exception:
        session['flash'] = 'Your choice is saved and will retry automatically. No sender-wide rule was added.'
    refreshed = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    vendor_review_state.reconcile(db, refreshed, json.loads(refreshed['context']))
    return redirect('/')

@app.route('/message-details/<int:decision_id>', methods=['POST'])
@login_required
def refresh_message_details(decision_id):
    import message_reviews
    try:
        message_reviews.refresh(decision_id, get_db())
        session['flash'] = 'Message details refreshed without marking it read.'
    except (ValueError, RuntimeError) as exc:
        session['flash'] = str(exc)
    except Exception:
        session['flash'] = 'Message details could not be loaded. The message remains protected; try again when the mailbox is available.'
    return redirect('/')


@app.route("/resolve/<int:decision_id>", methods=["POST"])
@login_required
def resolve(decision_id):
    db = get_db()
    row = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        abort(404)
    if row['kind'] not in ('vendor_mapping', 'message_review'):
        abort(400, 'Use the review action for this decision type.')
    if row["status"] != "pending":
        abort(409, "This decision has already been submitted. Refresh the page.")
    action = request.form.get("action")
    allowed = ("map", "skip") if row["kind"] == "vendor_mapping" else ("keep", "keep_brief", "trash", "skip")
    if action not in allowed:
        abort(400, "Unknown decision action.")
    if action == "skip" and row["kind"] != "vendor_mapping":
        db.execute("UPDATE decisions SET resolution=NULL WHERE id=? AND status='pending'", (decision_id,))
        db.commit()
        return redirect("/")
    resolution = {"action": action}
    if action == "map":
        bucket = (request.form.get("bucket_custom") or request.form.get("bucket") or "").strip()
        vendor = request.form.get("vendor_name", "").strip()
        if not bucket or not vendor or any(c in bucket + vendor for c in '\r\n"\\'):
            abort(400, "Enter a folder and vendor name without quotes or control characters.")
        resolution.update(bucket=bucket, vendor_name=vendor)
    changed = db.execute("UPDATE decisions SET status='resolved', resolution=?, resolved_at=? WHERE id=? AND status='pending' AND context=?", (json.dumps(resolution), datetime.now(timezone.utc).isoformat(), decision_id, row["context"]))
    db.commit()
    if changed.rowcount != 1:
        abort(409, "This decision changed. Refresh the page before trying again.")
    try:
        context = json.loads(row['context'] or '{}')
    except (ValueError, TypeError):
        context = {}
    if isinstance(context, dict) and context.get('rule_clarification'):
        abort(409, 'Clarify the full instruction before retrying.')
    try:
        session["flash"] = apply_decisions.apply_one(decision_id)
    except Exception as exc:
        db.execute("UPDATE decisions SET status='pending' WHERE id=?", (decision_id,))
        db.commit()
        session["flash"] = f"Could not apply this decision: {exc}. It is still pending."
    return redirect("/")


@app.route("/add-rule", methods=["POST"])
@login_required
def add_rule():
    db = get_db()
    rule_text = request.form.get("rule_text", "").strip()
    if rule_text:
        resolution = {"action": "free_text_rule", "text": rule_text}
        cur = db.execute(
            "INSERT INTO decisions (kind, summary, context, status, resolution, created_at, resolved_at) "
            "VALUES ('free_text_rule', ?, ?, 'resolved', ?, ?, ?)",
            (
                f"Rule: {rule_text[:80]}",
                "Submitted through the rule box.",
                json.dumps(resolution),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
        row_id = cur.lastrowid

        try:
            outcome = apply_decisions.apply_one(row_id)
            session["flash"] = f"Rule saved: {outcome}"
        except Exception as e:
            db.execute("UPDATE decisions SET status='pending' WHERE id=?", (row_id,))
            db.commit()
            session["flash"] = (
                f"Rule saved, but couldn't apply it right now ({e}). "
                "Use Retry on the decisions page."
            )
    return redirect("/")


def rule_revision(row):
    return hashlib.sha256(json.dumps([row['context'], row['resolution'], row['status']], ensure_ascii=True).encode()).hexdigest()


@app.route("/clarify-rule/<int:decision_id>", methods=["POST"])
@login_required
def clarify_rule(decision_id):
    db = get_db()
    row = db.execute("SELECT * FROM decisions WHERE id=? AND kind='free_text_rule'", (decision_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        context = json.loads(row['context'])
        resolution = json.loads(row['resolution'])
    except (ValueError, TypeError):
        abort(409, 'This instruction is not awaiting clarification.')
    if (row['status'] != 'pending' or not isinstance(context, dict) or not isinstance(resolution, dict)
            or not context.get('rule_clarification') or context.get('applied')
            or context.get('rule_proposal') or resolution.get('approved_proposal')):
        abort(409, 'This instruction is not awaiting clarification.')
    if request.form.get('revision') != rule_revision(row):
        abort(409, 'This instruction changed; refresh before editing.')
    text = request.form.get('rule_text', '').strip()
    if not text or len(text) > 20000 or '\x00' in text:
        abort(400, 'Enter the complete instruction, up to 20,000 characters.')
    selected_domain = request.form.get('observed_domain', '').strip().lower()
    if selected_domain:
        if selected_domain not in observed_rule_domains(db):
            abort(400, 'Choose an observed domain from the list, or leave it blank and enter the exact domain in your instruction.')
        text += '\nExact sender domain: ' + selected_domain + '.'
    context.pop('rule_clarification', None)
    resolution = {'action': 'free_text_rule', 'text': text}
    changed = db.execute("UPDATE decisions SET summary=?, context=?, resolution=?, status='resolved', resolved_at=? WHERE id=? AND context=? AND resolution=? AND status='pending'",
                         ('Rule: ' + text[:80], json.dumps(context), json.dumps(resolution), datetime.now(timezone.utc).isoformat(), decision_id, row['context'], row['resolution']))
    if changed.rowcount != 1:
        db.rollback()
        abort(409, 'This instruction changed; refresh before editing.')
    db.commit()
    try:
        session['flash'] = 'Instruction saved: ' + apply_decisions.apply_one(decision_id)
    except Exception:
        db.execute("UPDATE decisions SET status='pending' WHERE id=?", (decision_id,))
        db.commit()
        session['flash'] = 'Instruction saved. Drafting did not finish; it remains pending for retry.'
    return redirect('/')


@app.route("/review-rule/<int:decision_id>", methods=["POST"])
@login_required
def review_rule(decision_id):
    db = get_db()
    row = db.execute("SELECT * FROM decisions WHERE id=? AND kind='free_text_rule'", (decision_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        context = json.loads(row['context'])
        proposal = context['rule_proposal']
        resolution = json.loads(row['resolution'])
    except (ValueError, TypeError, KeyError):
        abort(400, 'No proposal is ready for review.')
    if request.form.get('proposal') != proposal['token']:
        abort(409, 'This proposal changed; refresh before approving.')
    if context.get('applied'):
        session['flash'] = context.get('outcome', 'Already handled')
        return redirect('/')
    action = request.form.get('action')
    if action == 'reject':
        if resolution.get('approved_proposal'):
            abort(409, 'This proposal was approved; retry completion before making another change.')
        context.update(applied=True, outcome='Proposal rejected; no rules changed')
        changed = db.execute("UPDATE decisions SET context=?, status='resolved' WHERE id=? AND context=? AND resolution=?", (json.dumps(context), decision_id, row['context'], row['resolution']))
        if changed.rowcount != 1:
            db.rollback()
            abort(409, 'This proposal changed; refresh before acting.')
        db.commit()
        session['flash'] = 'Proposal rejected; no rules changed.'
    elif action == 'approve':
        resolution['approved_proposal'] = proposal['token']
        changed = db.execute('UPDATE decisions SET resolution=? WHERE id=? AND context=? AND resolution=?', (json.dumps(resolution), decision_id, row['context'], row['resolution']))
        if changed.rowcount != 1:
            db.rollback()
            abort(409, 'This proposal changed; refresh before acting.')
        db.commit()
        try:
            session['flash'] = apply_decisions.apply_one(decision_id)
            db.execute("UPDATE decisions SET status='resolved' WHERE id=?", (decision_id,))
            db.commit()
        except Exception as exc:
            session['flash'] = f'Changes were not completed: {exc}. The proposal remains pending.'
    else:
        abort(400, 'Choose Approve or Reject.')
    return redirect('/')


@app.route("/retry-rule/<int:decision_id>", methods=["POST"])
@login_required
def retry_rule(decision_id):
    db = get_db()
    row = db.execute("SELECT * FROM decisions WHERE id=? AND kind='free_text_rule'", (decision_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        context = json.loads(row['context'] or '{}')
    except (ValueError, TypeError):
        context = {}
    if isinstance(context, dict) and context.get('rule_clarification'):
        abort(409, 'Clarify the full instruction before retrying.')
    try:
        session["flash"] = apply_decisions.apply_one(decision_id)
        latest = db.execute('SELECT context FROM decisions WHERE id=?', (decision_id,)).fetchone()
        if latest and json.loads(latest['context'] or '{}').get('applied'):
            db.execute("UPDATE decisions SET status='resolved' WHERE id=?", (decision_id,))
            db.commit()
    except Exception as exc:
        session["flash"] = f"Could not apply rule: {exc}. You can retry."
    return redirect("/")


def _unsubscribe_card(row, non_compliant=False, suggestion=None, related_handled=None):
    mechanism = "one-click unsubscribe" if row["one_click"] else ("unsubscribe link" if row["unsubscribe_url"] else ("email unsubscribe" if row["unsubscribe_mailto"] else "no unsubscribe mechanism found"))
    template = NON_COMPLIANT_CARD if non_compliant else UNSUBSCRIBE_CARD
    rendered = template.format(
        id=row["id"],
        display_name=html(row["display_name"] or row["sender_domain"]),
        sender_email=html(row["sender_email"] or row["sender_domain"]),
        message_count=row["message_count"],
        mechanism=mechanism,
        manual_link=(f'<p><a href="/subscription-messages/{row["id"]}">View emails</a></p>' + (f'<p><a href="/unsubscribe-link/{row["id"]}" target="_blank" rel="noopener noreferrer">Open sender’s unsubscribe page</a> <span class="hint">Complete any confirmation there.</span></p>' if row['unsubscribe_url'] else '')),
        result=html(session.pop("subscription_result_" + str(row["id"]), "")),
    )
    if related_handled:
        rendered = rendered.replace('</fieldset>', '</fieldset><p class="hint">Same display name as a handled sender: ' + ', '.join(html(domain) for domain in related_handled[:3]) + '. This is a different sending domain: ' + html(row['sender_domain']) + '. Its choice is separate.</p>')
    if suggestion and suggestion.get('action') in ('unsubscribe_block_marketing', 'unsubscribe', 'block_all', 'dismiss'):
        rendered = rendered.replace('value="" checked', 'value=""').replace('value="' + suggestion['action'] + '"', 'value="' + suggestion['action'] + '" checked')
        rendered = rendered.replace('</fieldset>', '</fieldset><p class="ai-suggestion">AI suggestion: ' + html(suggestion.get('reason', '')) + '</p>')
    return rendered


@app.route("/unsubscribe")
@login_required
def unsubscribe_page():
    import subscription_suggestions
    suggestions = {item['candidate_id']: item for item in subscription_suggestions.latest_recommendations()}
    db = get_db()
    non_compliant_rows = db.execute(
        "SELECT * FROM unsubscribe_candidates WHERE status = 'pending' AND non_compliant = 1 ORDER BY last_seen_at DESC"
    ).fetchall()
    pending_rows = db.execute(
        "SELECT * FROM unsubscribe_candidates WHERE status = 'pending' AND non_compliant = 0 ORDER BY last_seen_at DESC"
    ).fetchall()

    handled_names = {}
    for handled in db.execute("SELECT display_name,sender_domain FROM unsubscribe_candidates WHERE status IN ('resolved','unsubscribed') AND display_name IS NOT NULL"):
        if handled['display_name'].strip():
            handled_names.setdefault(handled['display_name'].strip().casefold(), []).append(handled['sender_domain'])
    non_compliant_banner = ""
    if non_compliant_rows:
        non_compliant_banner = NON_COMPLIANT_SECTION.format(
            cards="".join(_unsubscribe_card(r, non_compliant=True, suggestion=suggestions.get(r["id"])) for r in non_compliant_rows)
        )
    body = "".join(_unsubscribe_card(r, suggestion=suggestions.get(r["id"]), related_handled=handled_names.get((r["display_name"] or "").strip().casefold())) for r in pending_rows) if pending_rows else '<p class="empty">No unsubscribe candidates pending.</p>'
    return UNSUBSCRIBE_PAGE_TEMPLATE.format(
        icon=TAHOR_ICON,
        style=STYLE_BLOCK,
        header=tahor_header("unsubscribe"),
        interaction_script=subscription_bulk_ui.SCRIPT,
        flash=FLASH_BANNER.format(message=html(session.pop("flash", ""))) if session.get("flash") else "",
        count=len(non_compliant_rows) + len(pending_rows),
        bulk_controls=subscription_bulk_ui.BAR,
        non_compliant_banner=non_compliant_banner,
        blocked_senders="".join(
            f'<div class="card"><div class="summary">{html(row["sender_domain"])}</div>'
            f'<p>{"All mail" if row["rule"] == "block_all" else "Marketing only"}</p>'
            f'<form method="post" action="/unblock-sender"><input type="hidden" name="domain" value="{html(row["sender_domain"])}"><button type="submit">Remove block</button></form></div>'
            for row in db.execute("SELECT sender_domain,rule FROM sender_rules ORDER BY sender_domain")
        ) or '<p class="empty">No blocked senders.</p>',
        cards=body,
    )


@app.route('/subscription-messages/<int:candidate_id>', methods=['GET', 'POST'])
@login_required
def subscription_messages_page(candidate_id):
    import subscription_messages
    db = get_db()
    candidate = db.execute('SELECT * FROM unsubscribe_candidates WHERE id=?', (candidate_id,)).fetchone()
    if candidate is None:
        abort(404)
    notice = ''
    samples = tahor_db.get_subscription_samples(candidate_id)
    if request.method == 'POST' or not samples:
        try:
            state = subscription_messages.scan(db, candidate_id)
            notice = ('Finished checking the available folders.' if state.get('complete') else 'Checked another small group of folders. You can continue searching below.')
        except ValueError:
            notice = 'An exact sender address is needed before these messages can be searched.'
        except Exception:
            notice = 'The mailbox is temporarily unavailable. Existing messages remain unchanged; you can retry.'
    samples = tahor_db.get_subscription_samples(candidate_id)
    entries = []
    for sample in samples:
        received = sample.get('received_at') or sample.get('date') or ''
        age = ''
        try:
            age = f" · {max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(received)).days)} days ago"
        except (ValueError, TypeError):
            pass
        entries.append(f'<div class="card"><h2><a href="/subscription-message/{candidate_id}/{sample["id"]}">{html(sample.get("subject") or "(No subject)")}</a></h2><p>From: {html(sample.get("display_name") or "")} &lt;{html(sample.get("sender_email") or "")}&gt;</p><p>{html(received + age)}</p><p>{html(sample.get("mailbox") or "")}</p></div>')
    return ('<!doctype html><html><head><meta charset="utf-8"><title>Tahor — subscription emails</title>' + STYLE_BLOCK + '</head><body><main>' + tahor_header('unsubscribe') +
        '<p><a href="/unsubscribe">Back to subscriptions</a></p><h1>Emails from ' + html(candidate['display_name'] or candidate['sender_email'] or candidate['sender_domain']) +
        '</h1><p>Up to three recently captured messages from this subscription. Sender addresses are shown individually. Viewing leaves messages unread and does not load remote images.</p><p role="status">' + html(notice) + '</p>' +
        (''.join(entries) or '<p>No message samples have been captured yet. Search the mailbox to find recent examples.</p>') +
        f'<form method="post" action="/subscription-messages/{candidate_id}"><button type="submit">Find more emails</button></form><p class="hint">Search checks the inbox first, then a few folders at a time. It does not change any messages.</p></main></body></html>')


@app.route('/subscription-message/<int:candidate_id>/<int:sample_id>')
@login_required
def subscription_message_view(candidate_id, sample_id):
    import message_reviews
    samples = tahor_db.get_subscription_samples(candidate_id)
    sample = next((item for item in samples if item['id'] == sample_id), None)
    if sample is None:
        abort(404)
    try:
        details, body = message_reviews.read_message(sample)
    except (ValueError, RuntimeError):
        return 'This message moved or could not be identified safely. Return to its email list and search again, or open it in Fastmail.', 409
    except Exception:
        return 'The mailbox is temporarily unavailable. Your message remains unread and unchanged.', 503
    return ('<!doctype html><html><head><meta charset="utf-8"><title>Tahor — subscription message</title>' + STYLE_BLOCK + '</head><body><main>' + tahor_header('unsubscribe') +
        f'<p><a href="/subscription-messages/{candidate_id}">Back to this sender’s emails</a></p><h1>' + html(details.get('subject') or '(No subject)') +
        '</h1><p>From: ' + html(details.get('sender', '')) + '</p><p>Received: ' + html(details.get('received_at', '')) +
        '</p><p>This text view does not mark the email read. Remote images and attachments are not displayed.</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + html(body) + '</pre></main></body></html>')


@app.route('/unsubscribe/batches', methods=['GET', 'POST'])
@login_required
def unsubscribe_batches():
    import subscription_bulk
    if request.method == 'GET':
        return jsonify(subscription_bulk.recent_jobs())
    try:
        selections = json.loads(request.form.get('selections', '[]'))
        return jsonify(subscription_bulk.enqueue(selections, request.form.get('request_key', ''))), 202
    except (ValueError, TypeError):
        return jsonify(error='A selected subscription changed or already has a queued request. Reload to review its current state.'), 409


@app.route('/unsubscribe/batches/<job_id>')
@login_required
def unsubscribe_batch_status(job_id):
    import subscription_bulk
    try:
        return jsonify(subscription_bulk.get_job(job_id))
    except ValueError:
        abort(404)


@app.route('/unsubscribe/suggestions', methods=['POST'])
@login_required
def unsubscribe_suggest():
    import subscription_suggestions
    try:
        excluded = json.loads(request.form.get('exclude_ids', '[]'))
        return jsonify(subscription_suggestions.enqueue(exclude_ids=excluded)), 202
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.route('/unsubscribe/suggestions/<job_id>')
@login_required
def unsubscribe_suggestion_status(job_id):
    import subscription_suggestions
    try:
        return jsonify(subscription_suggestions.get_job(job_id))
    except ValueError:
        abort(404)


@app.route("/unsubscribe-link/<int:candidate_id>")
@login_required
def unsubscribe_link(candidate_id):
    row = get_db().execute("SELECT unsubscribe_url FROM unsubscribe_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row or not row['unsubscribe_url']:
        abort(404)
    from unsubscribe import validate_url
    try:
        target = validate_url(row['unsubscribe_url'])
    except Exception:
        abort(400, "This sender's unsubscribe link could not be safely opened.")
    response = redirect(target)
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.route("/unsubscribe/<int:candidate_id>", methods=["POST"])
@login_required
def unsubscribe_action(candidate_id):
    import subscription_bulk
    if subscription_bulk.active_candidate(candidate_id):
        abort(409, 'This subscription has a queued or unconfirmed request. Check its current result first.')
    db = get_db()
    row = db.execute("SELECT * FROM unsubscribe_candidates WHERE id = ?", (candidate_id,)).fetchone()
    action = request.form.get("action")
    if row is None:
        abort(404)
    if action not in ("unsubscribe", "unsubscribe_block_marketing", "block_all", "dismiss"):
        abort(400, "Unknown subscription action.")
    new_status = "resolved"
    outcome = "Subscription kept."
    unsubscribe_failed = False
    if row and action in ("unsubscribe", "unsubscribe_block_marketing", "block_all"):
        try:
            outcome = tahor_db.execute_unsubscribe(
                row,
                os.environ.get("FASTMAIL_EMAIL"),
                os.environ.get("FASTMAIL_APP_PASSWORD"),
                config.SMTP_HOST,
                config.SMTP_PORT,
            )
        except Exception as e:
            unsubscribe_failed = True
            from unsubscribe import describe_failure
            outcome = describe_failure(e)
        app.logger.info("unsubscribe %s (%s): %s", row["sender_domain"], action, outcome)
        if action == "unsubscribe":
            new_status = "pending" if unsubscribe_failed else "unsubscribed"  # watched: if this sender mails again, it resurfaces flagged non-compliant
    if row and action in ("unsubscribe_block_marketing", "block_all"):
        rule = "block_all" if action == "block_all" else "block_marketing"
        tahor_db.set_sender_rule(row["sender_domain"], rule)
        outcome += (" Marketing block saved; transactional mail remains allowed." if rule == "block_marketing" else " All-mail block saved, including transactional mail.")
        try:
            generate_sieve.refresh_sieve()
            outcome += " Sieve proposal updated on the decisions page."
        except Exception as exc:
            outcome += f" Sieve proposal could not be updated: {exc}. The worker block is active."
    db.execute("UPDATE unsubscribe_candidates SET status=?,non_compliant=0,unsubscribed_at=CASE WHEN ?='unsubscribed' THEN ? ELSE unsubscribed_at END WHERE id=?", (new_status,new_status,datetime.now(timezone.utc).isoformat(),candidate_id))
    db.commit()
    if request.headers.get("Accept") == "application/json":
        return jsonify(message=outcome, pending=new_status == "pending", failed=unsubscribe_failed, candidate_id=candidate_id)
    if new_status == "pending":
        session["subscription_result_" + str(candidate_id)] = outcome
    else:
        session["flash"] = (row["display_name"] or row["sender_domain"]) + ": " + outcome
    return redirect("/unsubscribe#subscription-" + str(candidate_id))


@app.route("/unblock-sender", methods=["POST"])
@login_required
def unblock_sender():
    domain = request.form.get("domain", "").strip().lower()
    if not tahor_db.get_sender_rule(domain):
        abort(404)
    tahor_db.clear_sender_rule(domain)
    try:
        generate_sieve.refresh_sieve()
        session["flash"] = "Block removed from the worker. Apply the updated Sieve proposal to remove the provider-side block too."
    except Exception as exc:
        session["flash"] = f"Worker block removed, but the Sieve proposal needs retry: {exc}"
    return redirect("/unsubscribe")


@app.route("/drafts")
@login_required
def drafts_page():
    return redirect('/settings#reply-rules')


@app.route("/status")
@login_required
def worker_status():
    snapshot = runtime_status.read_status()
    details = [("Classification policy", dict((key, label) for key, label, _ in AI_POLICY_OPTIONS).get(mailbox_settings.get_classify_mode(), mailbox_settings.get_classify_mode())),
               ("Last worker update", snapshot.get("updated_at", "Not recorded")),
               ("Last successful batch", snapshot.get("last_success_at", "Not recorded")),
               ("Applied in last batch", snapshot.get("last_batch_applied", "—")),
               ("Pending retry in last batch", snapshot.get("last_batch_pending", "—"))]
    rows = "".join(f'<tr><th style="text-align:left;padding:10px">{html(label)}</th><td>{html(value)}</td></tr>' for label, value in details)
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Tahor — status</title>{STYLE_BLOCK}</head><body><main>{tahor_header("status")}<h1>Worker status</h1><p>{html(runtime_status.describe_status(snapshot))}</p><div class="card"><table>{rows}</table></div><p class="hint">Speed is your preference. Temporary provider fallback does not change it. Refresh this page for the latest worker report.</p></main></body></html>'


@app.route("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return {"ok": True}


if __name__ == "__main__":
    init_db()
    # Bind to localhost only until OAuth is wired in -- never expose this
    # unauthenticated to the public internet.
    app.run(host="127.0.0.1", port=8420)
