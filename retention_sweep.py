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
    typ, data = search_uids(conn, "BEFORE", cutoff, "KEYWORD", keyword, "UNKEYWORD", "retention-forever", "UNKEYWORD", "retention-pending-review", "UNKEYWORD", "needs-attention", "OR", "SEEN", "UNKEYWORD", "reply-protected")
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
    paths = [p for p in list_all_paths(conn) if p not in SKIP_MAILBOXES]

    grand_found = grand_deleted = 0
    for path in paths:
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
