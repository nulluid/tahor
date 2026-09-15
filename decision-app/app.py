#!/usr/bin/env python3
"""
Tahor decision queue: a tiny web app so pending classification decisions
(new vendor mappings, ambiguous keep/trash calls, free-text rule requests)
can be resolved from a browser instead of a live session.

Data flow:
  the sweep scripts write rows into decisions.db when they hit something
  they can't decide alone -> this app lets you resolve them -> apply_decisions.py
  (a separate script, run by cron) reads resolved rows and either applies
  them directly (vendor mappings: pure data) or hands free-text rules to an
  LLM to draft the change.

No auth yet -- bind to 127.0.0.1 only until Google OAuth is wired in.
"""
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, g, redirect, request, session

DB_PATH = Path(__file__).parent / "decisions.db"

# DATA_DIR holds vendor_buckets.json, prompt.txt, and sieve.txt -- the same
# gitignored config files classify.py/config.py read from the repo root.
# Defaults to this repo (one directory up from decision-app/), but can
# point anywhere, including a separate private git repo, if you want your
# vendor/prompt/sieve data to have its own tracked history independent of
# this codebase.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
SIEVE_PATH = DATA_DIR / "sieve.txt"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8420")

ALLOWED_EMAIL = os.environ.get("ALLOWED_EMAIL")
if not ALLOWED_EMAIL:
    raise SystemExit("Set ALLOWED_EMAIL in your environment -- the one Google account allowed to sign in.")
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
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,             -- 'vendor_mapping' | 'free_text_rule' | 'needs_attention_review'
            summary TEXT NOT NULL,          -- human-readable one-liner shown on the page
            context TEXT,                   -- JSON: sample sender/subject/counts/etc.
            status TEXT NOT NULL DEFAULT 'pending',
            resolution TEXT,                -- JSON: what you chose
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tahor — pending decisions</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%230B2624'/%3E%3Cpath fill-rule='evenodd' fill='%230EA5A0' d='M50,14 C50,14 22,56 22,68 A28,28 0 1 0 78,68 C78,56 50,14 50,14 Z M33,53 L50,65 L67,53 L67,59 L50,71 L33,59 Z'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Source+Sans+3:wght@400;600&family=DM+Mono&display=swap">
<style>
  :root {{
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
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
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
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 36px 20px 72px;
    background: var(--ground);
    color: var(--ink);
    font-family: var(--text);
    font-size: 16px;
    line-height: 1.5;
  }}
  main {{ max-width: 640px; margin: 0 auto; }}
  header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 40px; }}
  header svg {{ width: 30px; height: 30px; color: var(--accent); flex: none; }}
  .wordmark {{ font-family: var(--display); font-size: 1.7rem; font-weight: 500; line-height: 1; letter-spacing: -0.01em; }}
  .hebrew {{ color: var(--muted); font-size: 1.05rem; margin-left: 10px; font-family: var(--text); }}
  h1, h2 {{ font-family: var(--display); font-weight: 500; letter-spacing: -0.01em; margin: 0 0 16px; }}
  h1 {{ font-size: 1.5rem; display: flex; align-items: baseline; gap: 10px; }}
  h2 {{ font-size: 1.25rem; }}
  .count {{ font-family: var(--text); font-size: 0.85rem; font-weight: 600; color: var(--accent); background: var(--raised); border: 1px solid var(--rule); border-radius: 999px; padding: 1px 10px; }}
  section {{ margin-top: 44px; padding-top: 28px; border-top: 1px solid var(--rule); }}
  .card {{ background: var(--raised); border: 1px solid var(--rule); border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; }}
  .card.sieve {{ border-color: var(--accent); background: color-mix(in srgb, var(--accent) 9%, var(--raised)); }}
  .card.sieve .summary {{ color: var(--accent); }}
  .card p {{ margin: 0 0 12px; }}
  .summary {{ font-weight: 600; font-size: 1.05rem; margin-bottom: 6px; }}
  .context {{ color: var(--muted); font-family: var(--mono); font-size: 0.82rem; line-height: 1.55; margin-bottom: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }}
  .fields {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }}
  .fields select {{ grid-column: 1 / -1; }}
  .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  select, input[type=text], textarea {{
    width: 100%;
    background: var(--well);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 6px;
    padding: 8px 10px;
    font: inherit;
    font-size: 0.95rem;
  }}
  textarea {{ resize: vertical; margin-bottom: 12px; }}
  ::placeholder {{ color: var(--faint); }}
  select:focus, input:focus, textarea:focus, button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  button {{
    background: transparent;
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 6px;
    padding: 8px 16px;
    font: inherit;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
  }}
  button:hover {{ border-color: var(--muted); }}
  button.primary {{ background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }}
  button.primary:hover {{ filter: brightness(1.08); }}
  button.trash {{ color: var(--trash); }}
  button.trash:hover {{ border-color: var(--trash); }}
  .empty {{ color: var(--muted); font-style: italic; margin: 0; }}
  .hint {{ color: var(--muted); font-size: 0.92rem; margin: 0 0 12px; }}
  pre {{
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
  }}
  @media (max-width: 480px) {{
    body {{ padding-top: 24px; }}
    header {{ margin-bottom: 28px; }}
    .fields {{ grid-template-columns: 1fr; }}
    .actions button {{ flex: 1 1 auto; }}
  }}
</style>
</head>
<body>
<main>
<header>
  <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path fill-rule="evenodd" fill="currentColor" d="M50,8 C50,8 18,54 18,68 A32,32 0 1 0 82,68 C82,54 50,8 50,8 Z M30,52 L50,66 L70,52 L70,59 L50,73 L30,59 Z"/></svg>
  <span class="wordmark">Tahor</span>
  <span class="hebrew" lang="he">טָהוֹר</span>
</header>
<h1>Pending decisions <span class="count">{count}</span></h1>
{sieve_banner}
{cards}
<section>
<h2>Add a free-text rule</h2>
<form method="post" action="/add-rule">
  <textarea name="rule_text" rows="3" placeholder="e.g. mail from this vendor should be trashed and unsubscribed, but keep any purchase receipts"></textarea>
  <button type="submit" class="primary">Submit rule for the model to draft</button>
</form>
</section>
<section>
<h2>Current recommended Sieve filter</h2>
<p class="hint">Paste this into your mail provider's Sieve editor (in Fastmail: Settings &rarr; Filters &amp; Rules &rarr; Edit custom Sieve code).</p>
<pre>{sieve_content}</pre>
</section>
</main>
</body>
</html>
"""

SIEVE_BANNER = """
<div class="card sieve">
  <div class="summary">Sieve filter update recommended</div>
  <div class="context">{context}</div>
  <p>Paste the updated filter (below) into your mail provider's Sieve editor, then confirm:</p>
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
      <input type="text" name="vendor_name" placeholder="Display name, e.g. Acme Corp">
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
        count=len(pending), sieve_banner=sieve_banner, cards=body, sieve_content=sieve_content
    )


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
                "Submitted directly via the rule box.",
                json.dumps({"action": "free_text_rule", "text": rule_text}),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    return redirect("/")


if __name__ == "__main__":
    init_db()
    # Bind to localhost only until OAuth is wired in -- never expose this
    # unauthenticated to the public internet.
    app.run(host="127.0.0.1", port=8420)
