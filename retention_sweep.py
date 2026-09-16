#!/usr/bin/env python3
"""
Delete mail whose retention window has passed, but only once it's been
read. Unread mail is never touched regardless of age or tier.

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
        return 0, 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).strftime("%d-%b-%Y")
    typ, data = conn.uid("SEARCH", None, "SEEN", "BEFORE", cutoff, "KEYWORD", keyword, "UNKEYWORD", "retention-forever", "UNKEYWORD", "retention-pending-review", "UNKEYWORD", "needs-attention")
    if typ != "OK" or not data or not data[0]:
        return 0, 0

    uids = data[0].split()
    if dry_run:
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
                print(f"{path}: {found} matched '{keyword}' (>{days}d, read) — {verb} {deleted if not dry_run else found}")
                grand_found += found
                grand_deleted += deleted

    conn.logout()
    if dry_run:
        print(f"\nDRY RUN. {grand_found} messages would be deleted.")
    else:
        print(f"\nDone. {grand_deleted}/{grand_found} matched messages deleted.")


if __name__ == "__main__":
    main()
