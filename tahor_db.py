#!/usr/bin/env python3
"""Shared decisions.db access for both app.py and the classification pipeline."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import os

DB_PATH = Path(os.environ.get("TAHOR_DB_PATH", Path(__file__).resolve().parent / "decisions.db"))  # fixed path, not relative -- worker and web app run from different dirs

SENDER_RULES = ("block_all", "block_marketing")  # block everything, or just marketing mail


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = sqlite3.connect(DB_PATH)
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
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
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
    conn.commit()
    conn.close()


def upsert_unsubscribe_candidate(sender_domain, sender_email, display_name, unsubscribe_url, unsubscribe_mailto, one_click):
    if not sender_domain:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    existing = conn.execute(
        "SELECT id, message_count FROM unsubscribe_candidates WHERE sender_domain = ?", (sender_domain,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE unsubscribe_candidates SET message_count = ?, last_seen_at = ?, "
            "unsubscribe_url = COALESCE(?, unsubscribe_url), unsubscribe_mailto = COALESCE(?, unsubscribe_mailto), "
            "one_click = MAX(one_click, ?) WHERE id = ?",
            (existing["message_count"] + 1, now, unsubscribe_url, unsubscribe_mailto, int(one_click), existing["id"]),
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


def create_reply_draft(original_message_id, recipient_email, subject, draft_body, trigger_reason):
    conn = get_db()
    conn.execute(
        "INSERT INTO reply_drafts (original_message_id, recipient_email, subject, draft_body, trigger_reason, "
        "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (original_message_id, recipient_email, subject, draft_body, trigger_reason, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
