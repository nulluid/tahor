#!/usr/bin/env python3
"""
Tahor decision queue: a tiny web app so pending classification decisions
(new vendor mappings, ambiguous keep/trash calls, free-text rule requests,
unsubscribe candidates, drafted replies) can be resolved from a browser
instead of a live session.

Data flow: the sweep scripts write rows into decisions.db when they hit
something they can't decide alone -> this app lets you resolve them ->
apply_decisions.py (a separate script, run by cron) reads resolved rows and
either applies them directly (vendor mappings: pure data) or hands free-text
rules to a model to draft the change.

No auth yet -- bind to 127.0.0.1 only until Google OAuth is wired in.
"""
import json
import os
import secrets
import smtplib
import sqlite3
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, g, redirect, request, session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
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
SECRET_KEY_PATH = Path(__file__).parent / ".flask_secret_key"
if not SECRET_KEY_PATH.exists():
    SECRET_KEY_PATH.write_text(secrets.token_hex(32))
    SECRET_KEY_PATH.chmod(0o600)

app = Flask(__name__)
app.secret_key = SECRET_KEY_PATH.read_text().strip()
app.config.update(SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"), SESSION_COOKIE_HTTPONLY=True)


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
    if request.args.get("state") != session.get("oauth_state"):
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
        return f"Access denied for {email}.", 403

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
  <span class="wordmark">Tahor</span>
  <span class="hebrew" lang="he">טָהוֹר</span>
  <nav class="nav-links">{nav_links}</nav>
</header>
"""


def tahor_header(current):
    """current: the page key that should be omitted from its own nav (a page
    doesn't link to itself). Keys: 'decisions', 'unsubscribe', 'drafts', 'settings'."""
    links = [
        ("decisions", "/", "Pending decisions"),
        ("unsubscribe", "/unsubscribe", "Unsubscribe"),
        ("drafts", "/drafts", "Drafts"),
        ("settings", "/settings", "Settings"),
    ]
    nav_links = "".join(f'<a class="nav-link" href="{href}">{label}</a>' for key, href, label in links if key != current)
    return TAHOR_HEADER.format(nav_links=nav_links)

# Shared <style> block for every page in this app -- kept as one constant so
# the settings page matches the decision-queue page's look exactly instead of
# drifting from a copy-pasted stylesheet.
STYLE_BLOCK = """
<style>
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
  .wordmark { font-family: var(--display); font-size: 1.7rem; font-weight: 500; line-height: 1; letter-spacing: -0.01em; }
  .hebrew { color: var(--muted); font-size: 1.05rem; margin-left: 10px; font-family: var(--text); }
  h1, h2 { font-family: var(--display); font-weight: 500; letter-spacing: -0.01em; margin: 0 0 16px; }
  h1 { font-size: 1.5rem; display: flex; align-items: baseline; gap: 10px; }
  h2 { font-size: 1.25rem; }
  .count { font-family: var(--text); font-size: 0.85rem; font-weight: 600; color: var(--accent); background: var(--raised); border: 1px solid var(--rule); border-radius: 999px; padding: 1px 10px; }
  section { margin-top: 44px; padding-top: 28px; border-top: 1px solid var(--rule); }
  .card { background: var(--raised); border: 1px solid var(--rule); border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; }
  .card.sieve { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 9%, var(--raised)); }
  .card.sieve .summary { color: var(--accent); }
  .card p { margin: 0 0 12px; }
  .summary { font-weight: 600; font-size: 1.05rem; margin-bottom: 6px; }
  .context { color: var(--muted); font-family: var(--mono); font-size: 0.82rem; line-height: 1.55; margin-bottom: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .fields { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }
  .fields select { grid-column: 1 / -1; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  select, input[type=text], textarea {
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
    background: transparent;
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 6px;
    padding: 8px 16px;
    font: inherit;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
  }
  button:hover { border-color: var(--muted); }
  button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  button.primary:hover { filter: brightness(1.08); }
  button.trash { color: var(--trash); }
  button.trash:hover { border-color: var(--trash); }
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
<h1>Pending decisions <span class="count">{count}</span></h1>
{sieve_banner}
{cards}
<section>
<h2>Add a free-text rule</h2>
<form method="post" action="/add-rule">
  <textarea name="rule_text" rows="3" placeholder="e.g. Kate Spade marketing should be trashed and unsubscribed, but keep any purchase receipts"></textarea>
  <button type="submit" class="primary">Submit rule for Gemini to draft</button>
</form>
</section>
<section>
<h2>Current recommended Sieve filter</h2>
<p class="hint">Paste this into Fastmail: Settings &rarr; Filters &amp; Rules &rarr; Edit custom Sieve code (third box).</p>
<pre>{sieve_content}</pre>
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
  <button type="submit" class="primary">Save</button>
</form>
<section>
<h2>Rule drafting model</h2>
<p class="hint">Used when you submit a free-text rule below on the main page. This runs rarely, so it's worth spending on quality over cost.</p>
<form method="post" action="/settings">
  <div class="mode-options">
    {rule_model_cards}
  </div>
  <button type="submit" class="primary">Save</button>
</form>
</section>
<section>
<h2>Reply drafting</h2>
<p class="hint">A message from any of these senders gets a drafted reply saved to Drafts for you to review and send yourself &mdash; nothing is ever sent automatically.</p>
{reply_triggers_list}
<form method="post" action="/add-reply-trigger">
  <div class="fields">
    <select name="trigger_type">
      <option value="sender_email">Specific address</option>
      <option value="sender_domain">Whole domain</option>
    </select>
    <input type="text" name="value" placeholder="e.g. boss@work.com or clientco.com">
  </div>
  <button type="submit" class="primary">Add trigger</button>
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
    <input type="radio" name="{field}" value="{value}"{checked}>
    <span class="summary">{label}</span>
    {active_badge}
  </div>
  <p class="context">{description}</p>
</label>
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
      <button type="submit" name="action" value="map" class="primary">File here</button>
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
<h1>Unsubscribe <span class="count">{count}</span></h1>
<p class="hint">Every sender seen with a List-Unsubscribe header, most recent first. Unsubscribing isn't always honored, so blocking is offered alongside it.</p>
{cards}
</main>
</body>
</html>
"""

UNSUBSCRIBE_CARD = """
<div class="card">
  <div class="summary">{display_name}</div>
  <div class="context">{sender_email} &middot; {message_count} message(s) &middot; {mechanism}</div>
  <form method="post" action="/unsubscribe/{id}">
    <div class="actions">
      <button type="submit" name="action" value="unsubscribe" class="primary">Unsubscribe</button>
      <button type="submit" name="action" value="unsubscribe_block_marketing">Unsubscribe + block marketing</button>
      <button type="submit" name="action" value="block_all" class="trash">Block entirely</button>
      <button type="submit" name="action" value="dismiss">Keep subscription</button>
    </div>
  </form>
</div>
"""

DRAFTS_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tahor — drafts</title>
<link rel="icon" type="image/svg+xml" href="{icon}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=DM+Mono&display=swap">
{style}
</head>
<body>
<main>
{header}
<h1>Reply drafts <span class="count">{count}</span></h1>
<p class="hint">Drafted from your reply triggers. Each one is also sitting in your Drafts folder, ready to edit and send &mdash; nothing here sends anything.</p>
{cards}
</main>
</body>
</html>
"""

DRAFT_CARD = """
<div class="card">
  <div class="summary">Re: {subject}</div>
  <div class="context">To {recipient_email} &middot; drafted {created_at}</div>
  <pre>{draft_body}</pre>
  <form method="post" action="/dismiss-draft/{id}">
    <button type="submit">Mark reviewed</button>
  </form>
</div>
"""


def known_buckets(db):
    rows = db.execute(
        "SELECT DISTINCT json_extract(resolution, '$.bucket') AS b FROM decisions WHERE resolution IS NOT NULL"
    ).fetchall()
    return sorted({r["b"] for r in rows if r["b"]})


@app.route("/")
@login_required
def index():
    db = get_db()
    pending = db.execute(
        "SELECT * FROM decisions WHERE status = 'pending' AND kind != 'sieve_update' ORDER BY created_at ASC"
    ).fetchall()

    buckets = known_buckets(db)
    bucket_options = "".join(f'<option value="{b}">{b}</option>' for b in buckets)

    cards = []
    for row in pending:
        ctx = row["context"] or ""
        if row["kind"] == "vendor_mapping":
            cards.append(
                CARD_VENDOR_MAPPING.format(
                    id=row["id"], summary=row["summary"], context=ctx, bucket_options=bucket_options
                )
            )
        else:
            cards.append(CARD_GENERIC.format(id=row["id"], summary=row["summary"], context=ctx))

    body = "".join(cards) if cards else '<p class="empty">Nothing pending — all caught up.</p>'

    sieve_row = db.execute(
        "SELECT * FROM decisions WHERE kind = 'sieve_update' AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    sieve_banner = (
        SIEVE_BANNER.format(id=sieve_row["id"], context=sieve_row["context"] or "")
        if sieve_row else ""
    )
    sieve_content = SIEVE_PATH.read_text() if SIEVE_PATH.exists() else "(not yet synced)"

    return PAGE_TEMPLATE.format(
        icon=TAHOR_ICON,
        style=STYLE_BLOCK,
        header=tahor_header("decisions"),
        count=len(pending),
        sieve_banner=sieve_banner,
        cards=body,
        sieve_content=sieve_content,
    )


MODE_LABELS = {"free": "Free", "paid": "Paid", "auto": "Auto"}
MODE_DESCRIPTIONS = {
    "free": "No cost, but capped at OpenRouter's free daily quota (about 1,000 requests a day) and slower.",
    "paid": "Fastest option, running at full measured throughput, but it costs real money (roughly $0.0003 per email).",
    "auto": "Balances the two: stays on free by default, and only spends money on paid capacity when the backlog would otherwise take over an hour to clear.",
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
            f"Paid mode is active, so it's running at full throughput regardless of backlog size.</p>"
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
        if "classify_mode" in request.form:
            mode = request.form.get("classify_mode", "")
            if mode in mailbox_settings.MODES:
                mailbox_settings.set_classify_mode(mode)
        elif "rule_model" in request.form:
            key = request.form.get("rule_model", "")
            if key in mailbox_settings.RULE_MODELS:
                mailbox_settings.set_rule_model(key)
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

    triggers = mailbox_settings.get_reply_triggers()
    if triggers:
        reply_triggers_list = "".join(
            REPLY_TRIGGER_ROW.format(
                value=t["value"],
                type=t["type"],
                type_label="Whole domain" if t["type"] == "sender_domain" else "Specific address",
            )
            for t in triggers
        )
    else:
        reply_triggers_list = '<p class="empty">No reply triggers configured yet.</p>'

    return SETTINGS_PAGE_TEMPLATE.format(
        icon=TAHOR_ICON,
        style=STYLE_BLOCK,
        header=tahor_header("settings"),
        status_line=_settings_status_line(current_mode),
        mode_cards=mode_cards,
        rule_model_cards=rule_model_cards,
        reply_triggers_list=reply_triggers_list,
    )


@app.route("/add-reply-trigger", methods=["POST"])
@login_required
def add_reply_trigger():
    trigger_type = request.form.get("trigger_type", "")
    value = request.form.get("value", "")
    if trigger_type in mailbox_settings.TRIGGER_TYPES and value.strip():
        mailbox_settings.add_reply_trigger(trigger_type, value)
    return redirect("/settings")


@app.route("/remove-reply-trigger", methods=["POST"])
@login_required
def remove_reply_trigger():
    mailbox_settings.remove_reply_trigger(request.form.get("trigger_type", ""), request.form.get("value", ""))
    return redirect("/settings")


@app.route("/dismiss-sieve/<int:decision_id>", methods=["POST"])
@login_required
def dismiss_sieve(decision_id):
    db = get_db()
    db.execute(
        "UPDATE decisions SET status = 'resolved', resolved_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), decision_id),
    )
    db.commit()
    return redirect("/")


@app.route("/resolve/<int:decision_id>", methods=["POST"])
@login_required
def resolve(decision_id):
    db = get_db()
    action = request.form.get("action")
    resolution = {"action": action}
    if action == "map":
        bucket = request.form.get("bucket_custom") or request.form.get("bucket")
        resolution["bucket"] = bucket
        resolution["vendor_name"] = request.form.get("vendor_name")
    db.execute(
        "UPDATE decisions SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ?",
        (json.dumps(resolution), datetime.now(timezone.utc).isoformat(), decision_id),
    )
    db.commit()
    return redirect("/")


@app.route("/add-rule", methods=["POST"])
@login_required
def add_rule():
    db = get_db()
    rule_text = request.form.get("rule_text", "").strip()
    if rule_text:
        db.execute(
            "INSERT INTO decisions (kind, summary, context, status, resolution, created_at, resolved_at) "
            "VALUES ('free_text_rule', ?, ?, 'resolved', ?, ?, ?)",
            (
                f"Rule: {rule_text[:80]}",
                "Submitted directly by Jason via the rule box.",
                json.dumps({"action": "free_text_rule", "text": rule_text}),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    return redirect("/")


def execute_unsubscribe(candidate):
    url = candidate["unsubscribe_url"]
    mailto = candidate["unsubscribe_mailto"]
    if candidate["one_click"] and url:
        resp = requests.post(url, data={"List-Unsubscribe": "One-Click"}, timeout=15)
        return f"one-click POST to {url}: {resp.status_code}"
    if mailto:
        if not (os.environ.get("FASTMAIL_EMAIL") and os.environ.get("FASTMAIL_APP_PASSWORD")):
            return "no SMTP credentials configured, mailto unsubscribe skipped"
        msg = MIMEText("")
        msg["From"] = config.email_address()
        msg["To"] = mailto
        msg["Subject"] = "unsubscribe"
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as smtp:
            smtp.login(config.email_address(), config.app_password())
            smtp.send_message(msg)
        return f"sent unsubscribe email to {mailto}"
    if url:
        resp = requests.get(url, timeout=15)
        return f"GET {url}: {resp.status_code}"
    return "no unsubscribe mechanism available"


@app.route("/unsubscribe")
@login_required
def unsubscribe_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM unsubscribe_candidates WHERE status = 'pending' ORDER BY last_seen_at DESC"
    ).fetchall()
    cards = []
    for row in rows:
        mechanism = "one-click unsubscribe" if row["one_click"] else ("unsubscribe link" if row["unsubscribe_url"] else ("email unsubscribe" if row["unsubscribe_mailto"] else "no unsubscribe mechanism found"))
        cards.append(
            UNSUBSCRIBE_CARD.format(
                id=row["id"],
                display_name=row["display_name"] or row["sender_domain"],
                sender_email=row["sender_email"] or row["sender_domain"],
                message_count=row["message_count"],
                mechanism=mechanism,
            )
        )
    body = "".join(cards) if cards else '<p class="empty">No unsubscribe candidates pending.</p>'
    return UNSUBSCRIBE_PAGE_TEMPLATE.format(
        icon=TAHOR_ICON, style=STYLE_BLOCK, header=tahor_header("unsubscribe"), count=len(rows), cards=body
    )


@app.route("/unsubscribe/<int:candidate_id>", methods=["POST"])
@login_required
def unsubscribe_action(candidate_id):
    db = get_db()
    row = db.execute("SELECT * FROM unsubscribe_candidates WHERE id = ?", (candidate_id,)).fetchone()
    action = request.form.get("action")
    if row and action in ("unsubscribe", "unsubscribe_block_marketing"):
        execute_unsubscribe(row)
    if row and action in ("unsubscribe_block_marketing", "block_all"):
        rule = "block_all" if action == "block_all" else "block_marketing"
        tahor_db.set_sender_rule(row["sender_domain"], rule)
    db.execute("UPDATE unsubscribe_candidates SET status = 'resolved' WHERE id = ?", (candidate_id,))
    db.commit()
    return redirect("/unsubscribe")


@app.route("/drafts")
@login_required
def drafts_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM reply_drafts WHERE status = 'pending' ORDER BY created_at DESC"
    ).fetchall()
    cards = [
        DRAFT_CARD.format(
            id=row["id"],
            subject=row["subject"],
            recipient_email=row["recipient_email"],
            created_at=row["created_at"][:16].replace("T", " "),
            draft_body=row["draft_body"],
        )
        for row in rows
    ]
    body = "".join(cards) if cards else '<p class="empty">No drafts waiting for review.</p>'
    return DRAFTS_PAGE_TEMPLATE.format(
        icon=TAHOR_ICON, style=STYLE_BLOCK, header=tahor_header("drafts"), count=len(rows), cards=body
    )


@app.route("/dismiss-draft/<int:draft_id>", methods=["POST"])
@login_required
def dismiss_draft(draft_id):
    db = get_db()
    db.execute(
        "UPDATE reply_drafts SET status = 'reviewed', resolved_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), draft_id),
    )
    db.commit()
    return redirect("/drafts")


if __name__ == "__main__":
    init_db()
    # Bind to localhost only until OAuth is wired in -- never expose this
    # unauthenticated to the public internet.
    app.run(host="127.0.0.1", port=8420)
