#!/usr/bin/env python3
"""
File already-classified mail out of the inbox once it's had a fair chance
to be seen: read and older than FILING_READ_MIN_AGE_DAYS, or still unread
past FILING_UNREAD_MIN_AGE_DAYS. Filing this on arrival would mean it's
never seen at all. The configurable defaults are three days for read mail
and seven days for unread mail.

Usage:
  python3 filing_sweep.py [--dry-run]

Vendor routing comes from vendor_buckets.json (see vendor_buckets.example.json):
registrable-domain label -> [bucket, display name]. An unmapped sender files
under "<root>/_Unsorted/<label>" instead of blocking, and is printed so the
table can grow.
"""
import email.utils
import imaplib
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
import tahor_db
import mailbox_settings

CATEGORY_KEYWORDS = ["category-receipt", "category-statement", "category-government-tax"]


def connect():
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=60)
    conn.login(config.email_address(), config.app_password())
    return conn


def vendor_for(from_header, buckets):
    _, addr = email.utils.parseaddr(from_header or "")
    domain = re.sub(r"^www\.", "", addr.split("@")[-1].lower() if "@" in addr else "")
    parts = domain.split(".") if domain else []
    # The registrable label is the second-to-last segment, not the leftmost
    # one, or every subdomain (notification.example.com) becomes its own vendor.
    label = (parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")).lower()
    if domain in buckets:
        return buckets[domain]
    if label in buckets:
        return buckets[label]
    return ("_Unsorted", domain if domain else "Unknown")


def ensure_folder(conn, path, created):
    if path in created:
        return
    # LIST rather than SELECT to probe existence — a failed SELECT drops the
    # session out of the selected state, breaking whatever comes after it.
    typ, data = conn.list('""', f'"{path}"')
    if typ != "OK" or not data or not data[0]:
        typ, _ = conn.create(f'"{path}"')
        if typ != "OK":
            raise RuntimeError(f"Could not create filing destination {path!r}")
    created.add(path)


def main():
    dry_run = "--dry-run" in sys.argv
    buckets = config.vendor_buckets()
    root = config.filing_root()
    grace = mailbox_settings.get_inbox_grace_days()
    read_min_age, unread_min_age = grace["read"], grace["unread"]

    conn = connect()
    typ, _ = conn.select('"INBOX"', readonly=dry_run)
    if typ != "OK":
        sys.exit("Could not select INBOX.")

    read_cutoff = (datetime.now(timezone.utc) - timedelta(days=read_min_age)).strftime("%d-%b-%Y")
    unread_cutoff = (datetime.now(timezone.utc) - timedelta(days=unread_min_age)).strftime("%d-%b-%Y")

    read_criteria = ("SEEN", "BEFORE", read_cutoff) if read_min_age > 0 else ("SEEN",)
    unread_criteria = ("UNSEEN", "BEFORE", unread_cutoff) if unread_min_age > 0 else ("UNSEEN",)
    candidates = set()
    for kw in CATEGORY_KEYWORDS:
        for criteria in (read_criteria, unread_criteria):
            typ, data = conn.uid("SEARCH", None, *criteria, "KEYWORD", kw)
            if typ != "OK":
                conn.logout()
                raise RuntimeError("Filing search failed; no messages moved")
            if typ == "OK" and data and data[0]:
                candidates.update(data[0].split())

    if not candidates:
        print("Nothing to file.")
        conn.logout()
        return

    by_dest = defaultdict(list)
    unsorted_labels = set()
    failures = 0
    for uid in candidates:
        typ, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if typ != "OK" or not msg_data or not msg_data[0]:
            failures += 1
            continue
        header_blob = msg_data[0][1].decode(errors="replace")
        from_header = header_blob.split(":", 1)[-1].strip() if ":" in header_blob else header_blob
        bucket, vendor = vendor_for(from_header, buckets)
        if bucket == "_Unsorted":
            unsorted_labels.add(vendor)
            if not dry_run and vendor != "Unknown":
                tahor_db.queue_vendor_mapping(vendor)
        by_dest[f"{root}/{bucket}/{vendor}"].append(uid)

    capabilities = {c.decode().upper() if isinstance(c, bytes) else c.upper() for c in conn.capabilities}
    if not dry_run and "MOVE" not in capabilities:
        raise RuntimeError("Filing requires IMAP MOVE to avoid copying or deleting unrelated mail")
    created, total_moved = set(), 0
    for dest, uids in sorted(by_dest.items()):
        verb = "would move" if dry_run else "moving"
        print(f"{dest}: {verb} {len(uids)} message(s)")
        if dry_run:
            continue
        ensure_folder(conn, dest, created)
        for uid in uids:
            typ, _ = conn.uid("MOVE", uid, f'"{dest}"')
            if typ == "OK":
                total_moved += 1
            else:
                failures += 1
                print(f"  FAILED to move uid {uid.decode()} to {dest}")
    conn.logout()

    if unsorted_labels:
        print(f"\n{len(unsorted_labels)} unmapped sender(s), add to vendor_buckets.json: {sorted(unsorted_labels)}")
    total = sum(len(v) for v in by_dest.values())
    if dry_run:
        print(f"\nDRY RUN. {total} messages would be filed across {len(by_dest)} folder(s).")
    else:
        print(f"\nDone. {total_moved} messages filed.")
    if failures:
        raise RuntimeError(f"Filing incomplete: {failures} message operation(s) failed; retry the sweep")


if __name__ == "__main__":
    main()
