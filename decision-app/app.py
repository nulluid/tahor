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
from flask import Flask, g, redirect, request, session, abort

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apply_decisions
import generate_sieve
import runtime_status
import config
import reply_rules
import provider_bridge
import mailbox_settings
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
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"
    if response.mimetype == "text/html" and not response.is_streamed:
        body = response.get_data(as_text=True)
        if '<form ' in body:
            token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
            field = f'<input type="hidden" name="csrf_token" value="{html(token)}">'
            body = re.sub(r'(<form\b[^>]*method="post"[^>]*>)', lambda match: match[0] + field, body, flags=re.I)
            response.set_data(body)
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
  <textarea name="rule_text" rows="3" placeholder="e.g. Kate Spade marketing should be trashed and unsubscribed, but keep any purchase receipts"></textarea>
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
<title>Tahor — classification mode</title>
<link rel="icon" type="image/svg+xml" href="{icon}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Source+Sans+3:wght@400;600&family=DM+Mono&display=swap">
{style}
</head>
<body>
<main>
{header}
<h1>Classification mode</h1>
{status_line}
<form method="post" action="/settings">
  <div class="mode-options">
    {mode_cards}
  </div>
</form>
<section>
<h2>Time in the inbox</h2>
<p class="hint">Keep read and unread messages in the inbox before filing them into folders. These delays run from delivery and do not delay classification, trash deletion, or retention cleanup.</p>
<form method="post" action="/settings">
  <input type="hidden" name="inbox_grace" value="1">
  <div class="fields">
    <label>Read mail (days)<input type="number" name="inbox_read_days" min="0" max="3650" value="{inbox_read_days}" required></label>
    <label>Unread mail (days)<input type="number" name="inbox_unread_days" min="0" max="3650" value="{inbox_unread_days}" required></label>
  </div>
  <button type="submit" class="primary">Save inbox timing</button>
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
<h2>Rule drafting model</h2>
<p class="hint">Used when you submit a free-text rule below on the main page. This runs rarely, so it's worth spending on quality over cost.</p>
<form method="post" action="/settings">
  <div class="mode-options">
    {rule_model_cards}
  </div>
</form>
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
<p class="hint">Model used to write and verify these replies. Choose a model known for natural English prose; each reply requires multiple model calls.</p>
<form method="post" action="/settings">
  <div class="mode-options">
    {reply_model_cards}
  </div>
</form>
<h3>Free backup for reply writing</h3>
<p class="hint">If the primary writer is unavailable, use this free model for both writing and verification. Retry the primary on new drafting work after a five-minute cooldown. A rejected reply stays pending; Tahor never substitutes another paid model. Disable backup to keep work pending until the primary recovers.</p>
<p class="hint">Writing is disabled until you choose a model. All available routes require provider zero data retention and prohibit data collection; if no eligible endpoint is available, work stays pending. Free backups are optional. Retired or unverified routes are disabled rather than replaced silently.</p>
<form method="post" action="/settings">
  <label>Free backup model <select name="reply_backup_model">{reply_backup_options}</select></label>
  <button type="submit">Save free backup</button>
</form>
</section>
</main>
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
  <form method="post" action="/resolve/{id}">
    <div class="fields">
      <select name="bucket">
        <option value="">-- choose or type below --</option>
        {bucket_options}
      </select>
      <input type="text" name="bucket_custom" placeholder="or new bucket, e.g. Shopping/Retail">
      <input type="text" name="vendor_name" placeholder="Display name, e.g. Kate Spade">
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
  <form method="post" action="/resolve/{id}">
    <div class="actions">
      <button type="submit" name="action" value="keep" class="primary">Keep</button>
      <button type="submit" name="action" value="trash" class="trash">Trash</button>
      <button type="submit" name="action" value="skip">Skip for now</button>
    </div>
  </form>
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
<p class="hint">Every sender seen with a List-Unsubscribe header, most recent first. Whichever action you pick removes it from this list. Unsubscribing isn't always honored, so blocking is offered alongside it -- "block entirely" unsubscribes too, then blocks going forward regardless.</p>
{non_compliant_banner}
{cards}
<section><h2>Blocked senders</h2>{blocked_senders}</section>
</main>
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
<div class="card">
  <div class="summary">{display_name}</div>
  <div class="context">{sender_email} &middot; {message_count} message(s) &middot; {mechanism}</div>
  <form method="post" action="/unsubscribe/{id}">
    <div class="actions">
      <button type="submit" name="action" value="unsubscribe" class="primary">Unsubscribe</button>
      <button type="submit" name="action" value="unsubscribe_block_marketing">Unsubscribe + block marketing</button>
      <button type="submit" name="action" value="block_all" class="trash">Unsubscribe + block entirely</button>
      <button type="submit" name="action" value="dismiss">Keep subscription</button>
    </div>
  </form>
</div>
"""

NON_COMPLIANT_CARD = """
<div class="card warn">
  <div class="summary">{display_name}</div>
  <div class="context">{sender_email} &middot; {message_count} message(s) &middot; sent again after you unsubscribed</div>
  <form method="post" action="/unsubscribe/{id}">
    <div class="actions">
      <button type="submit" name="action" value="unsubscribe_block_marketing" class="primary">Block marketing (keep receipts, etc.)</button>
      <button type="submit" name="action" value="block_all" class="trash">Block entirely</button>
      <button type="submit" name="action" value="dismiss">Leave unsubscribed, don't block</button>
    </div>
  </form>
</div>
"""


def decision_context(row):
    raw = row["context"] or ""
    try:
        context = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(context, dict):
        return str(context)
    if context.get("note"):
        return str(context["note"])
    if row["kind"] == "vendor_mapping":
        return f'Sender: {context.get("sender_label", "unknown")}. Choose where future receipts should be filed.'
    if row["kind"] == "message_review":
        return f'In {context.get("mailbox", "your mailbox")}. Protected from retention cleanup until you decide.'
    return str(context.get("outcome") or context.get("explanation") or "Ready for your review.")


def known_buckets(db):
    buckets = set()
    for row in db.execute("SELECT resolution FROM decisions WHERE resolution IS NOT NULL"):
        try:
            value = json.loads(row["resolution"])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("bucket"), str):
            buckets.add(value["bucket"])
    return sorted(buckets)


@app.route("/")
@login_required
def index():
    db = get_db()
    pending = db.execute(
        "SELECT * FROM decisions WHERE status = 'pending' AND kind != 'sieve_update' ORDER BY created_at ASC"
    ).fetchall()

    buckets = known_buckets(db)
    bucket_options = "".join(f'<option value="{html(b)}">{html(b)}</option>' for b in buckets)

    cards = []
    for row in pending:
        ctx = decision_context(row)
        if row["kind"] == "free_text_rule":
            cards.append(f'<div class="card"><div class="summary">{html(row["summary"])}</div><form method="post" action="/retry-rule/{row["id"]}"><button type="submit">Retry rule</button></form></div>')
        elif row["kind"] == "vendor_mapping":
            cards.append(
                CARD_VENDOR_MAPPING.format(
                    id=row["id"], summary=html(row["summary"]), context=html(ctx), bucket_options=bucket_options
                )
            )
        else:
            cards.append(CARD_GENERIC.format(id=row["id"], summary=html(row["summary"]), context=html(ctx)))

    body = "".join(cards) if cards else '<p class="empty">Nothing pending — all caught up.</p>'

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
        count=len(pending),
        flash=flash,
        sieve_banner=sieve_banner,
        cards=body,
        sieve_content=html(sieve_content),
    )


MODE_LABELS = {"free": "Free", "paid": "Paid", "auto": "Auto"}
MODE_DESCRIPTIONS = {
    "free": "No paid classification requests. Speed and availability depend on the provider’s free quota.",
    "paid": "Paid capacity for clearing a backlog faster. Provider usage charges apply; failed requests can temporarily fall back to free.",
    "auto": "Balances free and paid capacity using estimated backlog and throughput. The one-hour target is an estimate, not a guarantee.",
}


def _settings_status_line(current_mode):
    backlog_estimate, _ = mailbox_settings.get_cached_backlog(mailbox_settings.BACKLOG_REFRESH_SECONDS)
    if backlog_estimate is None:
        return ""
    free_rate = mailbox_settings.recent_free_rate()
    if free_rate <= 0:
        return ""
    hours_at_free = backlog_estimate / free_rate / 3600

    if current_mode == "paid":
        return (
            f'<p class="hint">Backlog estimate: ~{backlog_estimate} messages. '
            f"Paid mode is selected. The Status page shows actual processing and retries.</p>"
        )
    if current_mode == "free":
        return (
            f'<p class="hint">Backlog estimate: ~{backlog_estimate} messages, '
            f"would clear in ~{hours_at_free:.1f}h at the current free rate.</p>"
        )

    free_count, paid_count = mailbox_settings.decide_backend_split(
        backlog_estimate, free_rate, mailbox_settings.DISPLAY_BATCH_SIZE
    )
    if paid_count == 0:
        return (
            f'<p class="hint">Auto mode: currently running free (no paid spend), '
            f"backlog (~{backlog_estimate} messages) would clear in ~{hours_at_free:.1f}h at the free rate.</p>"
        )
    paid_fraction = paid_count / mailbox_settings.DISPLAY_BATCH_SIZE
    dollars_per_hour = paid_fraction * mailbox_settings.PAID_RATE_MSGS_PER_SEC * 3600 * mailbox_settings.COST_PER_PAID_MSG
    return (
        f'<p class="hint">Auto mode: currently blending in paid (~{paid_fraction * 100:.0f}% of each batch), '
        f"roughly ${dollars_per_hour:.2f}/hour in paid spend to keep the backlog "
        f"(~{backlog_estimate} messages) under an hour.</p>"
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        if "inbox_grace" in request.form:
            try:
                mailbox_settings.set_inbox_grace_days(request.form.get("inbox_read_days", ""), request.form.get("inbox_unread_days", ""))
            except ValueError as exc:
                abort(400, str(exc))
        elif "classify_mode" in request.form:
            mode = request.form.get("classify_mode", "")
            if mode in mailbox_settings.MODES:
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
        return redirect("/settings")

    current_mode = mailbox_settings.get_classify_mode()
    mode_cards = "".join(
        MODE_OPTION.format(
            field="classify_mode",
            value=m,
            label=MODE_LABELS[m],
            description=MODE_DESCRIPTIONS[m],
            active_class=" active" if m == current_mode else "",
            checked=" checked" if m == current_mode else "",
            active_badge='<span class="count">current</span>' if m == current_mode else "",
        )
        for m in mailbox_settings.MODES
    )

    current_rule_model = mailbox_settings.get_rule_model()
    rule_model_cards = "".join(
        MODE_OPTION.format(
            field="rule_model",
            value=key,
            label=backend["label"],
            description=f"Model: {backend['model']}",
            active_class=" active" if key == current_rule_model else "",
            checked=" checked" if key == current_rule_model else "",
            active_badge='<span class="count">current</span>' if key == current_rule_model else "",
        )
        for key, backend in mailbox_settings.RULE_MODELS.items()
    )

    current_reply_model = mailbox_settings.get_reply_model()
    reply_model_cards = "".join(
        MODE_OPTION.format(
            field="reply_model",
            value=key,
            label=backend["label"],
            description=f"Model: {backend['model']}",
            active_class=" active" if key == current_reply_model else "",
            checked=" checked" if key == current_reply_model else "",
            active_badge='<span class="count">current</span>' if key == current_reply_model else "",
        )
        for key, backend in mailbox_settings.REPLY_MODELS.items()
    )

    provider = provider_bridge.status()
    return SETTINGS_PAGE_TEMPLATE.format(
        icon=TAHOR_ICON,
        style=STYLE_BLOCK,
        header=tahor_header("settings"),
        inbox_read_days=mailbox_settings.get_inbox_grace_days()["read"],
        inbox_unread_days=mailbox_settings.get_inbox_grace_days()["unread"],
        status_line=_settings_status_line(current_mode),
        mode_cards=mode_cards,
        rule_model_cards=rule_model_cards,
        reply_model_cards=reply_model_cards,
        reply_backup_options=('<option value="none"'+(' selected' if mailbox_settings.get_reply_backup_model() == 'none' else '')+'>Disabled — keep replies pending</option>')+"".join(f'<option value="{html(key)}"{" selected" if key == mailbox_settings.get_reply_backup_model() else ""}>{html(backend["label"])}</option>' for key, backend in mailbox_settings.free_reply_models().items()),
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
        cards.append(f'''<div class="card"><h3>{html(rule['name'])}</h3><p>{"Enabled" if rule.get('enabled', True) else "Paused"}</p>
<p>{html(rule['match'])}</p><p>{html(rule['instructions'])}</p><pre>{html(rule.get('signature',''))}</pre>
<details><summary>Matched senders ({len(senders)}) and opt-outs</summary><p class="hint">Opting out stops future drafts from that sender for this rule. Existing drafts stay in your mailbox; mail protection and normal inbox timing are unchanged.</p>{''.join(sender_rows) or '<p>No matched senders yet.</p>'}</details>
<details><summary>Edit rule</summary><form method="post" action="/reply-rules/save">
<input type="hidden" name="rule_id" value="{identifier}">
<p><label>Name<br><input name="name" value="{html(rule['name'])}" required maxlength="120"></label></p>
<p><label>Match using<br><select name="match_type">{options}</select></label></p>
<p><label>Which messages?<br><textarea name="match" rows="3" required maxlength="3000">{html(rule['match'])}</textarea></label></p>
<p><label>Reply directions<br><textarea name="instructions" rows="4" required maxlength="6000">{html(rule['instructions'])}</textarea></label></p>
<p><label>Filing folder after inbox timing<br><input name="filing_folder" maxlength="250" value="{html(rule.get('filing_folder',''))}"></label></p>
<p><label>Signature<br><textarea name="signature" rows="2" maxlength="300">{html(rule.get('signature',''))}</textarea></label></p>
<p><label>Maximum sentences<br><select name="max_sentences">{sentence_options}</select></label></p>
<button type="submit">Save changes</button></form></details>
<form method="post" action="/reply-rules/toggle"><input type="hidden" name="rule_id" value="{identifier}"><button name="enabled" value="{'0' if rule.get('enabled', True) else '1'}">{'Pause rule' if rule.get('enabled', True) else 'Enable rule'}</button></form></div>''')
    return ''.join(cards) or '<p>No reply rules yet. Add one below.</p>'


@app.route('/reply-rules/save', methods=['POST'])
@login_required
def save_reply_rule():
    try:
        reply_rules.save_rule(request.form.get('name',''), request.form.get('match_type',''), request.form.get('match',''), request.form.get('instructions',''), request.form.get('signature',''), request.form.get('max_sentences','3'), request.form.get('rule_id') or None, filing_folder=request.form.get('filing_folder','').strip())
    except ValueError as error:
        abort(400, str(error))
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


@app.route("/resolve/<int:decision_id>", methods=["POST"])
@login_required
def resolve(decision_id):
    db = get_db()
    row = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        abort(404)
    action = request.form.get("action")
    allowed = ("map", "skip") if row["kind"] == "vendor_mapping" else ("keep", "trash", "skip")
    if action not in allowed:
        abort(400, "Unknown decision action.")
    if action == "skip" and row["kind"] != "vendor_mapping":
        return redirect("/")
    resolution = {"action": action}
    if action == "map":
        bucket = (request.form.get("bucket_custom") or request.form.get("bucket") or "").strip()
        vendor = request.form.get("vendor_name", "").strip()
        if not bucket or not vendor or any(c in bucket + vendor for c in '\r\n"\\'):
            abort(400, "Enter a folder and vendor name without quotes or control characters.")
        resolution.update(bucket=bucket, vendor_name=vendor)
    db.execute("UPDATE decisions SET status='resolved', resolution=?, resolved_at=? WHERE id=?", (json.dumps(resolution), datetime.now(timezone.utc).isoformat(), decision_id))
    db.commit()
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
            session["flash"] = f"Rule applied: {outcome}"
        except Exception as e:
            db.execute("UPDATE decisions SET status='pending' WHERE id=?", (row_id,))
            db.commit()
            session["flash"] = (
                f"Rule saved, but couldn't apply it right now ({e}). "
                "Use Retry on the decisions page."
            )
    return redirect("/")


@app.route("/retry-rule/<int:decision_id>", methods=["POST"])
@login_required
def retry_rule(decision_id):
    db = get_db()
    row = db.execute("SELECT * FROM decisions WHERE id=? AND kind='free_text_rule'", (decision_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        session["flash"] = apply_decisions.apply_one(decision_id)
        db.execute("UPDATE decisions SET status='resolved' WHERE id=?", (decision_id,))
        db.commit()
    except Exception as exc:
        session["flash"] = f"Could not apply rule: {exc}. You can retry."
    return redirect("/")


def _unsubscribe_card(row, non_compliant=False):
    mechanism = "one-click unsubscribe" if row["one_click"] else ("unsubscribe link" if row["unsubscribe_url"] else ("email unsubscribe" if row["unsubscribe_mailto"] else "no unsubscribe mechanism found"))
    template = NON_COMPLIANT_CARD if non_compliant else UNSUBSCRIBE_CARD
    return template.format(
        id=row["id"],
        display_name=html(row["display_name"] or row["sender_domain"]),
        sender_email=html(row["sender_email"] or row["sender_domain"]),
        message_count=row["message_count"],
        mechanism=mechanism,
    )


@app.route("/unsubscribe")
@login_required
def unsubscribe_page():
    db = get_db()
    non_compliant_rows = db.execute(
        "SELECT * FROM unsubscribe_candidates WHERE status = 'pending' AND non_compliant = 1 ORDER BY last_seen_at DESC"
    ).fetchall()
    pending_rows = db.execute(
        "SELECT * FROM unsubscribe_candidates WHERE status = 'pending' AND non_compliant = 0 ORDER BY last_seen_at DESC"
    ).fetchall()

    non_compliant_banner = ""
    if non_compliant_rows:
        non_compliant_banner = NON_COMPLIANT_SECTION.format(
            cards="".join(_unsubscribe_card(r, non_compliant=True) for r in non_compliant_rows)
        )
    body = "".join(_unsubscribe_card(r) for r in pending_rows) if pending_rows else '<p class="empty">No unsubscribe candidates pending.</p>'
    return UNSUBSCRIBE_PAGE_TEMPLATE.format(
        icon=TAHOR_ICON,
        style=STYLE_BLOCK,
        header=tahor_header("unsubscribe"),
        flash=FLASH_BANNER.format(message=html(session.pop("flash", ""))) if session.get("flash") else "",
        count=len(non_compliant_rows) + len(pending_rows),
        non_compliant_banner=non_compliant_banner,
        blocked_senders="".join(
            f'<div class="card"><div class="summary">{html(row["sender_domain"])}</div>'
            f'<p>{"All mail" if row["rule"] == "block_all" else "Marketing only"}</p>'
            f'<form method="post" action="/unblock-sender"><input type="hidden" name="domain" value="{html(row["sender_domain"])}"><button type="submit">Remove block</button></form></div>'
            for row in db.execute("SELECT sender_domain,rule FROM sender_rules ORDER BY sender_domain")
        ) or '<p class="empty">No blocked senders.</p>',
        cards=body,
    )


@app.route("/unsubscribe/<int:candidate_id>", methods=["POST"])
@login_required
def unsubscribe_action(candidate_id):
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
            outcome = f"Unsubscribe failed: {e}"  # best-effort: a dead unsubscribe link or SMTP failure shouldn't block the rest of the action
        app.logger.info("unsubscribe %s (%s): %s", row["sender_domain"], action, outcome)
        if action == "unsubscribe":
            new_status = "pending" if unsubscribe_failed else "unsubscribed"  # watched: if this sender mails again, it resurfaces flagged non-compliant
    if row and action in ("unsubscribe_block_marketing", "block_all"):
        rule = "block_all" if action == "block_all" else "block_marketing"
        tahor_db.set_sender_rule(row["sender_domain"], rule)
        outcome += "; sender block saved."
        try:
            generate_sieve.refresh_sieve()
            outcome += " Sieve proposal updated on the decisions page."
        except Exception as exc:
            outcome += f" Sieve proposal could not be updated: {exc}. The worker block is active."
    db.execute("UPDATE unsubscribe_candidates SET status=?,non_compliant=0,unsubscribed_at=CASE WHEN ?='unsubscribed' THEN ? ELSE unsubscribed_at END WHERE id=?", (new_status,new_status,datetime.now(timezone.utc).isoformat(),candidate_id))
    db.commit()
    session["flash"] = outcome
    return redirect("/unsubscribe")


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
    details = [("Configured speed", mailbox_settings.get_classify_mode()),
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
