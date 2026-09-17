#!/usr/bin/env python3
"""Shared decisions.db access for both app.py and the classification pipeline."""
import json
import smtplib
import sqlite3
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
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
    conn.execute("BEGIN IMMEDIATE")
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
    if "unsubscribed_at" not in existing_cols:
        conn.execute("ALTER TABLE unsubscribe_candidates ADD COLUMN unsubscribed_at TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS unsubscribe_seen (sender_domain TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(sender_domain,message_id))")
    conn.execute("""CREATE TABLE IF NOT EXISTS subscription_message_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL REFERENCES unsubscribe_candidates(id) ON DELETE CASCADE,
        mailbox TEXT NOT NULL, message_id TEXT NOT NULL, uid TEXT, uidvalidity TEXT,
        sender_email TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '', message_date TEXT NOT NULL DEFAULT '',
        received_at TEXT NOT NULL DEFAULT '', received_epoch REAL NOT NULL DEFAULT 0,
        captured_at TEXT NOT NULL,
        UNIQUE(candidate_id, message_id)
    )""")
    conn.execute('CREATE INDEX IF NOT EXISTS subscription_sample_candidate ON subscription_message_samples(candidate_id,received_epoch DESC)')
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
    conn.execute("CREATE TABLE IF NOT EXISTS reply_rule_matches (rule_id TEXT NOT NULL, message_id TEXT NOT NULL, sender TEXT NOT NULL, matched_at TEXT NOT NULL, PRIMARY KEY(rule_id,message_id))")
    reply_cols = {row[1] for row in conn.execute("PRAGMA table_info(reply_drafts)")}
    if "thread_root" not in reply_cols:
        conn.execute("ALTER TABLE reply_drafts ADD COLUMN thread_root TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE TABLE IF NOT EXISTS sender_samples (sender_email TEXT PRIMARY KEY, message_id TEXT NOT NULL, created_at TEXT NOT NULL)")
    # A saved exact-domain block already settles the subscription decision,
    # including blocks created through natural-language rules or prior releases.
    _reconcile_blocked_subscriptions(conn)
    conn.commit()
    conn.close()


def _reconcile_blocked_subscriptions(conn):
    conn.execute("UPDATE unsubscribe_candidates SET status='resolved',non_compliant=0 WHERE status IN ('pending','unsubscribed') AND EXISTS (SELECT 1 FROM sender_rules s WHERE s.sender_domain=unsubscribe_candidates.sender_domain AND s.rule IN ('block_marketing','block_all'))")


def upsert_unsubscribe_candidate(sender_domain, sender_email, display_name, unsubscribe_url, unsubscribe_mailto, one_click, message_id=None, received_at=None, is_marketing=False, metadata=None):
    if not sender_domain:
        return
    sender_domain = sender_domain.lower()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM unsubscribe_candidates WHERE sender_domain=?", (sender_domain,)).fetchone()
            if message_id:
                inserted = conn.execute("INSERT OR IGNORE INTO unsubscribe_seen(sender_domain,message_id) VALUES (?,?)", (sender_domain, message_id))
                if not inserted.rowcount:
                    if existing and metadata:
                        sample = dict(metadata, message_id=message_id, sender_email=sender_email, display_name=display_name, received_at=received_at or metadata.get('received_at', ''))
                        _record_subscription_sample(conn, existing['id'], sample)
                    return existing['id'] if existing else None
            if existing:
                resend = False
                if existing["status"] == "unsubscribed" and existing["unsubscribed_at"] and received_at and is_marketing:
                    try:
                        resend = datetime.fromisoformat(received_at) > datetime.fromisoformat(existing["unsubscribed_at"])
                    except (TypeError, ValueError):
                        pass
                conn.execute(
                    "UPDATE unsubscribe_candidates SET message_count=message_count+1,last_seen_at=?,"
                    "unsubscribe_url=COALESCE(?,unsubscribe_url),unsubscribe_mailto=COALESCE(?,unsubscribe_mailto),"
                    "one_click=?,status=?,non_compliant=non_compliant OR ? WHERE id=?",
                    (now, unsubscribe_url, unsubscribe_mailto, int(one_click) if unsubscribe_url else existing["one_click"],
                     "pending" if resend else existing["status"], int(resend), existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO unsubscribe_candidates(sender_domain,sender_email,display_name,unsubscribe_url,unsubscribe_mailto,one_click,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
                    (sender_domain,sender_email,display_name,unsubscribe_url,unsubscribe_mailto,int(one_click),now,now),
                )
            candidate_id = existing['id'] if existing else conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            _reconcile_blocked_subscriptions(conn)
            if metadata:
                sample = dict(metadata, message_id=message_id or metadata.get('message_id'), sender_email=sender_email, display_name=display_name, received_at=received_at or metadata.get('received_at', ''))
                _record_subscription_sample(conn, candidate_id, sample)
            return candidate_id
    finally:
        conn.close()


def _sample_text(value, limit):
    if not isinstance(value, str):
        return ''
    return ''.join(char if ord(char) >= 32 and ord(char) != 127 else ' ' for char in value).strip()[:limit]


def _sample_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _record_subscription_sample(conn, candidate_id, metadata):
    if not isinstance(metadata, dict):
        return None
    candidate = conn.execute('SELECT sender_domain FROM unsubscribe_candidates WHERE id=?', (candidate_id,)).fetchone()
    mailbox, identifier, sender = (metadata.get(key) for key in ('mailbox', 'message_id', 'sender_email'))
    if (candidate is None or not all(isinstance(value, str) and value for value in (mailbox, identifier, sender))
            or len(mailbox) > 1024 or len(identifier) > 2048 or len(sender) > 320
            or any(ord(char) < 32 or ord(char) == 127 for char in mailbox + identifier + sender)
            or sender.count('@') != 1 or any(char.isspace() for char in sender)
            or sender.rsplit('@', 1)[1].lower() != candidate['sender_domain'].lower()):
        return None
    uid, validity = (str(metadata.get(key) or '') for key in ('uid', 'uidvalidity'))
    if not all(value.isascii() and value.isdigit() and len(value) <= 10 and 0 < int(value) <= 4294967295
               for value in (uid, validity)):
        # Invalid shortcuts must fall back to exact Message-ID lookup rather
        # than preventing the rest of this batch from being processed.
        uid = validity = None
    received = _sample_date(metadata.get('received_at'))
    sent = _sample_date(metadata.get('date'))
    date = received or sent
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT INTO subscription_message_samples
        (candidate_id,mailbox,message_id,uid,uidvalidity,sender_email,display_name,subject,message_date,received_at,received_epoch,captured_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(candidate_id,message_id) DO UPDATE SET
        mailbox=excluded.mailbox,uid=excluded.uid,uidvalidity=excluded.uidvalidity,
        sender_email=excluded.sender_email,display_name=excluded.display_name,subject=excluded.subject,
        message_date=excluded.message_date,received_at=excluded.received_at,
        received_epoch=excluded.received_epoch,captured_at=excluded.captured_at""",
        (candidate_id,mailbox,identifier,uid,validity,sender,
         _sample_text(metadata.get('display_name'), 500), _sample_text(metadata.get('subject'), 1000),
         _sample_text(metadata.get('date'), 200), received.isoformat() if received else '',
         date.timestamp() if date else 0, now))
    conn.execute("""DELETE FROM subscription_message_samples WHERE candidate_id=? AND id NOT IN
        (SELECT id FROM subscription_message_samples WHERE candidate_id=?
         ORDER BY received_epoch DESC,captured_at DESC,id DESC LIMIT 3)""", (candidate_id,candidate_id))
    saved = conn.execute('SELECT id FROM subscription_message_samples WHERE candidate_id=? AND message_id=?', (candidate_id,identifier)).fetchone()
    return saved['id'] if saved else None


def record_subscription_sample(candidate_id, metadata, busy_timeout_ms=None):
    """Retain at most three recent exact message identities, never message bodies."""
    conn = get_db()
    if busy_timeout_ms is not None:
        conn.execute("PRAGMA busy_timeout=" + str(max(0, min(int(busy_timeout_ms), 30000))))
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            return _record_subscription_sample(conn, candidate_id, metadata)
    finally:
        conn.close()


def get_subscription_samples(candidate_id, busy_timeout_ms=None):
    conn = get_db()
    if busy_timeout_ms is not None:
        conn.execute("PRAGMA busy_timeout=" + str(max(0, min(int(busy_timeout_ms), 30000))))
    try:
        rows = conn.execute("""SELECT id,candidate_id,mailbox,message_id,uid,uidvalidity,
            sender_email,display_name,subject,message_date AS date,received_at,captured_at
            FROM subscription_message_samples WHERE candidate_id=?
            ORDER BY received_epoch DESC,captured_at DESC,id DESC""", (candidate_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
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
    _reconcile_blocked_subscriptions(conn)
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


def execute_unsubscribe(candidate, from_addr, app_password, smtp_host=None, smtp_port=None):
    import config
    from unsubscribe import execute
    return execute(candidate, from_addr, app_password,
                   config.SMTP_HOST if smtp_host is None else smtp_host,
                   config.SMTP_PORT if smtp_port is None else smtp_port)


def queue_message_review(mailbox, message_id, subject, uid=None, uidvalidity=None, metadata=None):
    values = {"mailbox": mailbox, "message_id": message_id}
    if uid and uidvalidity:
        values.update(uid=uid, uidvalidity=uidvalidity)
    for key in ('sender', 'date', 'received_at', 'snippet'):
        value = (metadata or {}).get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()[:500]
    values['subject'] = subject or ''
    conn = get_db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # Applied rows gain outcome fields and metadata can improve later;
            # neither changes the identity of the owner's original decision.
            for row in conn.execute("SELECT id,context FROM decisions WHERE kind='message_review'"):
                try:
                    previous = json.loads(row['context'] or '{}')
                except (ValueError, TypeError):
                    continue
                if isinstance(previous, dict) and previous.get('mailbox') == mailbox and previous.get('message_id') == message_id:
                    return
            conn.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES ('message_review',?,?, 'pending',?)", (subject or '', json.dumps(values), datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()


def get_reply_draft_for_thread(thread_root):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM reply_drafts WHERE thread_root=? ORDER BY id LIMIT 1", (thread_root,)).fetchone()
    finally:
        conn.close()


def prepare_reply_draft(original_message_id, thread_root, recipient_email, subject, draft_body, trigger_reason):
    conn = get_db()
    try:
        with conn:
            conn.execute("INSERT INTO reply_drafts(original_message_id,thread_root,recipient_email,subject,draft_body,trigger_reason,status,created_at) VALUES (?,?,?,?,?,?,'preparing',?)", (original_message_id,thread_root,recipient_email,subject,draft_body,trigger_reason,datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()


def finish_reply_draft(thread_root):
    conn = get_db()
    try:
        with conn:
            conn.execute("UPDATE reply_drafts SET status='pending' WHERE thread_root=? AND status='preparing'", (thread_root,))
    finally:
        conn.close()


def queue_vendor_mapping(sender_domain, metadata=None):
    metadata = metadata or {}
    values = {'sender_label': sender_domain}
    sender = str(metadata.get('sender_email') or '').strip().lower()
    if sender and sender.count('@') == 1 and not any(c.isspace() or ord(c) < 32 for c in sender):
        values.update(sender_email=sender, routing_key=sender)
    for key in ('display_name', 'subject', 'date', 'received_at', 'excerpt', 'suggested_bucket', 'suggested_vendor'):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()[:500]
    sample = {key: values[key] for key in ('subject', 'date', 'received_at', 'excerpt') if values.get(key)}
    for key in ('mailbox', 'message_id', 'uid', 'uidvalidity'):
        value = metadata.get(key)
        if isinstance(value, (str, int)) and str(value):
            sample[key] = str(value)
    if sample:
        values['samples'] = [sample]
    conn = get_db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute("SELECT id,context,status FROM decisions WHERE kind='vendor_mapping'"):
                try:
                    previous = json.loads(row['context'] or '{}')
                except (TypeError, ValueError):
                    continue
                if not isinstance(previous, dict):
                    continue
                same = previous.get('routing_key', previous.get('sender_label')) == values.get('routing_key', sender_domain)
                upgrade = (sender and row['status'] == 'pending' and not previous.get('routing_key') and previous.get('sender_label') == sender_domain)
                if same or upgrade:
                    if row['status'] == 'pending' and metadata:
                        samples = previous.get('samples', []) if isinstance(previous.get('samples', []), list) else []
                        if previous.get('suggestion_source') == 'ai':
                            values.pop('suggested_vendor', None)
                            values.pop('suggested_bucket', None)
                        if sample and sample not in samples:
                            samples.append(sample)
                            previous.pop('suggestion_version', None)
                            previous.pop('suggestion_status', None)
                        previous.update(values)
                        previous['samples'] = samples[-3:]
                        label = values.get('display_name') or sender or sender_domain
                        conn.execute('UPDATE decisions SET context=?, summary=? WHERE id=?', (json.dumps(previous), f'Choose a filing folder for {label}', row['id']))
                    return
            label = values.get('display_name') or sender or sender_domain
            conn.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES ('vendor_mapping',?,?, 'pending',?)", (f'Choose a filing folder for {label}', json.dumps(values), datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()


def has_sender_sample(sender_email):
    conn = get_db()
    try:
        return conn.execute("SELECT 1 FROM sender_samples WHERE sender_email=?", (sender_email.strip().lower(),)).fetchone() is not None
    finally:
        conn.close()


def record_sender_sample(sender_email, message_id):
    conn = get_db()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO sender_samples(sender_email,message_id,created_at) VALUES (?,?,?)", (sender_email.strip().lower(), message_id, datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()


def record_reply_rule_match(rule_id, message_id, sender):
    conn = get_db()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO reply_rule_matches VALUES (?,?,?,?)", (rule_id, message_id, sender.lower(), datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()


def reply_rule_senders(rule_id):
    conn = get_db()
    try:
        return conn.execute("SELECT sender, COUNT(*) AS messages, MAX(matched_at) AS last_match FROM reply_rule_matches WHERE rule_id=? GROUP BY sender ORDER BY last_match DESC", (rule_id,)).fetchall()
    finally:
        conn.close()


def relocate_vendor_samples(source, destination, message_ids):
    """Keep queued sample links accurate after a confirmed IMAP MOVE."""
    identifiers = set(message_ids)
    if not identifiers:
        return
    conn = get_db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute("SELECT id,context FROM decisions WHERE kind='vendor_mapping' AND status='pending'"):
                try:
                    context = json.loads(row['context'] or '{}')
                except (ValueError, TypeError):
                    continue
                if not isinstance(context, dict) or not isinstance(context.get('samples'), list):
                    continue
                changed = False
                for sample in context['samples']:
                    if isinstance(sample, dict) and sample.get('mailbox') == source and sample.get('message_id') in identifiers:
                        sample['mailbox'] = destination
                        sample.pop('uid', None)
                        sample.pop('uidvalidity', None)
                        changed = True
                if changed:
                    conn.execute('UPDATE decisions SET context=? WHERE id=?', (json.dumps(context), row['id']))
    finally:
        conn.close()
