#!/usr/bin/env python3
"""Shared decisions.db access for both app.py and the classification pipeline."""
import json
import smtplib
import sqlite3
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
import os

DB_PATH = Path(os.environ.get("TAHOR_DB_PATH", Path(__file__).resolve().parent / "decisions.db"))  # fixed path, not relative -- worker and web app run from different dirs

SENDER_RULES = ("block_all", "block_marketing")  # block everything, or just marketing mail


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            context TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            resolution TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS unsubscribe_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_domain TEXT NOT NULL UNIQUE,
            sender_email TEXT,
            display_name TEXT,
            unsubscribe_url TEXT,
            unsubscribe_mailto TEXT,
            one_click INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            non_compliant INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(unsubscribe_candidates)")}
    if "non_compliant" not in existing_cols:
        conn.execute("ALTER TABLE unsubscribe_candidates ADD COLUMN non_compliant INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sender_rules (
            sender_domain TEXT PRIMARY KEY,
            rule TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_message_id TEXT NOT NULL,
            thread_root TEXT NOT NULL,
            recipient_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            draft_body TEXT NOT NULL,
            trigger_reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    reply_cols = {row[1] for row in conn.execute("PRAGMA table_info(reply_drafts)")}
    if "thread_root" not in reply_cols:
        conn.execute("ALTER TABLE reply_drafts ADD COLUMN thread_root TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()


def upsert_unsubscribe_candidate(sender_domain, sender_email, display_name, unsubscribe_url, unsubscribe_mailto, one_click):
    if not sender_domain:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    existing = conn.execute(
        "SELECT id, message_count, status FROM unsubscribe_candidates WHERE sender_domain = ?", (sender_domain,)
    ).fetchone()
    if existing:
        # A sender that mails again after status='unsubscribed' (set only by the
        # plain "Unsubscribe" action, see app.py) didn't honor it -- resurface as
        # pending and flag non_compliant so the unsubscribe page can call it out.
        resend_after_unsubscribe = existing["status"] == "unsubscribed"
        new_status = "pending" if resend_after_unsubscribe else existing["status"]
        conn.execute(
            "UPDATE unsubscribe_candidates SET message_count = ?, last_seen_at = ?, "
            "unsubscribe_url = COALESCE(?, unsubscribe_url), unsubscribe_mailto = COALESCE(?, unsubscribe_mailto), "
            "one_click = MAX(one_click, ?), status = ?, non_compliant = non_compliant OR ? WHERE id = ?",
            (
                existing["message_count"] + 1, now, unsubscribe_url, unsubscribe_mailto, int(one_click),
                new_status, int(resend_after_unsubscribe), existing["id"],
            ),
        )
    else:
        conn.execute(
            "INSERT INTO unsubscribe_candidates "
            "(sender_domain, sender_email, display_name, unsubscribe_url, unsubscribe_mailto, one_click, "
            "message_count, status, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?)",
            (sender_domain, sender_email, display_name, unsubscribe_url, unsubscribe_mailto, int(one_click), now, now),
        )
    conn.commit()
    conn.close()


def get_sender_rule(sender_domain):
    if not sender_domain:
        return None
    conn = get_db()
    row = conn.execute("SELECT rule FROM sender_rules WHERE sender_domain = ?", (sender_domain,)).fetchone()
    conn.close()
    return row["rule"] if row else None


def set_sender_rule(sender_domain, rule):
    if rule not in SENDER_RULES:
        raise ValueError(f"Unknown sender rule {rule!r}, choose from {SENDER_RULES}")
    conn = get_db()
    conn.execute(
        "INSERT INTO sender_rules (sender_domain, rule, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(sender_domain) DO UPDATE SET rule = excluded.rule, created_at = excluded.created_at",
        (sender_domain, rule, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def clear_sender_rule(sender_domain):
    conn = get_db()
    conn.execute("DELETE FROM sender_rules WHERE sender_domain = ?", (sender_domain,))
    conn.commit()
    conn.close()


def create_reply_draft(original_message_id, thread_root, recipient_email, subject, draft_body, trigger_reason):
    conn = get_db()
    conn.execute(
        "INSERT INTO reply_drafts (original_message_id, thread_root, recipient_email, subject, draft_body, "
        "trigger_reason, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (original_message_id, thread_root, recipient_email, subject, draft_body, trigger_reason, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def has_reply_draft_for_thread(thread_root):
    conn = get_db()
    row = conn.execute("SELECT 1 FROM reply_drafts WHERE thread_root = ? LIMIT 1", (thread_root,)).fetchone()
    conn.close()
    return row is not None


def get_unsubscribe_candidate(sender_domain):
    conn = get_db()
    row = conn.execute("SELECT * FROM unsubscribe_candidates WHERE sender_domain = ?", (sender_domain,)).fetchone()
    conn.close()
    return row


def execute_unsubscribe(candidate, from_addr, app_password, smtp_host="smtp.fastmail.com", smtp_port=465):
    from unsubscribe import execute
    return execute(candidate, from_addr, app_password, smtp_host, smtp_port)


def queue_message_review(mailbox, message_id, subject):
    context = json.dumps({"mailbox": mailbox, "message_id": message_id})
    conn = get_db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT 1 FROM decisions WHERE kind='message_review' AND context=?", (context,)).fetchone()
            if exists is None:
                conn.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES ('message_review',?,?, 'pending',?)", (subject, context, datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()
