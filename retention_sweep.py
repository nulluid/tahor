#!/usr/bin/env python3
"""
Delete expired mail regardless of read status. Filing grace does not
delay retention or deletion of messages explicitly classified as trash.

Usage:
  python3 retention_sweep.py [--dry-run]

Tiers, overridable via RETENTION_<TIER>_DAYS:
  retention-transient  ->  7 days
  retention-standard   ->  1095 days
retention-forever and retention-pending-review are never touched.
"""
import sys
from datetime import datetime, timedelta, timezone

import config
import digest_retention
import mailbox_settings
from mailbox_paths import quote_mailbox
from message_expiry import metadata, expired
from mailbox_search import search_uids

TIERS = [
    ("retention-transient", config.retention_days("transient", 7)),
    ("retention-standard", config.retention_days("standard", 365 * 3)),
]

SKIP_MAILBOXES = {"Trash", "Spam", "Sent", "Drafts", "Archive"}


def connect():
    import imaplib

    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=60)
    conn.login(config.email_address(), config.app_password())
    return conn


def list_all_paths(conn):
    typ, folders = conn.list()
    paths = []
    for f in folders:
        line = f.decode()
        flags_part = line[line.index("(") + 1 : line.index(")")]
        if "\\Noselect" in flags_part:
            continue
        rest = line[line.index(")") + 1 :].strip()
        paths.append(rest.split(" ", 1)[1].strip().strip('"'))
    return paths


def sweep_mailbox(conn, path, keyword, cutoff_days, dry_run):
    typ, _ = conn.select('"' + path.replace('\\', '\\\\').replace('"', '\\"') + '"', readonly=dry_run)
    if typ != "OK":
        raise RuntimeError("Retention could not select a mailbox")

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=cutoff_days)).strftime("%d-%b-%Y")
    typ, data = search_uids(conn, "BEFORE", cutoff, "KEYWORD", keyword, "UNKEYWORD", digest_retention.KEYWORD, "UNKEYWORD", "retention-short-lived", "UNKEYWORD", "retention-forever", "UNKEYWORD", "retention-pending-review", "UNKEYWORD", "needs-attention", "OR", "SEEN", "UNKEYWORD", "reply-protected")
    if typ != "OK":
        raise RuntimeError("Retention search failed; no messages changed in this pass")
    if not data or not data[0]:
        return 0, 0

    return delete_uids(conn, data[0].split(), dry_run)


def sweep_trash(conn, path, dry_run=False):
    typ, _ = conn.select('"' + path.replace('\\', '\\\\').replace('"', '\\"') + '"', readonly=dry_run)
    if typ != "OK":
        raise RuntimeError("Trash cleanup could not select a mailbox")
    typ, data = search_uids(conn, "KEYWORD", "delete-pending", "UNKEYWORD", "retention-forever", "UNKEYWORD", "retention-pending-review", "UNKEYWORD", "needs-attention", "OR", "SEEN", "UNKEYWORD", "reply-protected")
    if typ != "OK":
        raise RuntimeError("Trash cleanup search failed")
    return delete_uids(conn, data[0].split() if data and data[0] else [], dry_run)


def sweep_short_lived(conn, path, dry_run=False, now=None):
    """Apply the owner's explicit Keep briefly choice in any selectable folder."""
    if conn.select(quote_mailbox(path), readonly=dry_run)[0] != 'OK':
        raise RuntimeError('Brief retention could not select a mailbox')
    status, rows = search_uids(conn, 'KEYWORD', 'retention-short-lived', 'UNFLAGGED',
                               'UNKEYWORD', 'retention-forever', 'UNKEYWORD', 'retention-pending-review',
                               'UNKEYWORD', 'needs-attention', 'UNKEYWORD', 'reply-protected')
    if status != 'OK':
        raise RuntimeError('Brief retention search failed')
    now = now or datetime.now(timezone.utc)
    grace = mailbox_settings.get_inbox_grace_days()
    found = deleted = 0
    for uid in rows[0].split() if rows and rows[0] else []:
        status, items = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE)')
        if status != 'OK':
            raise RuntimeError('Brief retention metadata unavailable')
        _, flags, date = metadata(items, uid)
        if b'retention-short-lived' not in flags or not expired(flags, date, now, grace):
            continue
        status, items = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE)')
        if status != 'OK':
            raise RuntimeError('Brief retention read state unavailable')
        _, flags, current_date = metadata(items, uid)
        if current_date != date or b'retention-short-lived' not in flags or not expired(flags, date, now, grace):
            continue
        matched, removed = delete_uids(conn, [uid], dry_run)
        found += matched
        deleted += removed
    return found, deleted


def delete_uids(conn, uids, dry_run):
    if dry_run or not uids:
        return len(uids), 0

    capabilities = {c.decode().upper() if isinstance(c, bytes) else c.upper() for c in conn.capabilities}
    if "UIDPLUS" not in capabilities:
        raise RuntimeError("Retention requires UIDPLUS for targeted deletion; no messages changed")
    deleted = 0
    for uid in uids:
        typ, _ = conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        if typ == "OK":
            typ, _ = conn.uid("EXPUNGE", uid)
            if typ != "OK":
                conn.uid("STORE", uid, "-FLAGS", "(\\Deleted)")
                raise RuntimeError("Targeted retention deletion failed")
            deleted += 1
    return len(uids), deleted


def main():
    dry_run = "--dry-run" in sys.argv
    conn = connect()
    all_paths = list_all_paths(conn)
    records = digest_retention.known_digests()

    grand_found = grand_deleted = 0
    for path in all_paths:
        found, deleted = digest_retention.sweep(conn, path, records, dry_run, delete_uids)
        grand_found += found
        grand_deleted += deleted
        if found:
            print(f"{path}: {found} expired Tahor digest(s), {deleted} permanently deleted")
        found, deleted = sweep_short_lived(conn, path, dry_run)
        grand_found += found
        grand_deleted += deleted
        if found:
            print(f"{path}: {found} expired brief notice(s), {deleted} permanently deleted")
        if path in SKIP_MAILBOXES:
            continue
        for keyword, days in TIERS:
            found, deleted = sweep_mailbox(conn, path, keyword, days, dry_run)
            if found:
                verb = "would delete" if dry_run else "deleted"
                print(f"{path}: {found} matched '{keyword}' (>{days}d) — {verb} {deleted if not dry_run else found}")
                grand_found += found
                grand_deleted += deleted

    conn.logout()
    if dry_run:
        print(f"\nDRY RUN. {grand_found} messages would be deleted.")
    else:
        print(f"\nDone. {grand_deleted}/{grand_found} matched messages deleted.")
        if grand_deleted != grand_found:
            raise RuntimeError("Retention incomplete: some messages could not be marked for deletion; retry the sweep")


if __name__ == "__main__":
    main()
