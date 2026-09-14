#!/usr/bin/env python3
"""
Set or clear IMAP keywords on messages, addressed by Message-ID rather than
UID so the same ops file works across reruns.

Usage:
  python3 keyword_tool.py ops.json

ops.json: a JSON array of
  {
    "mailbox": "Finance/Statements",
    "message_id": "<abc123@example.com>",
    "add": ["receipt", "retention-forever"],
    "remove": ["unclassified"]
  }
"""
import imaplib
import json
import sys
from collections import defaultdict

import config


def connect():
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    conn.login(config.email_address(), config.app_password())
    return conn


def find_uid(conn, message_id):
    # IMAP quoted-string syntax requires escaping backslash and embedded
    # double quotes (RFC 3501) — some real-world Message-ID headers have them.
    escaped = message_id.replace("\\", "\\\\").replace('"', '\\"')
    typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{escaped}"')
    if typ != "OK" or not data or not data[0]:
        return None
    uids = data[0].split()
    return uids[0].decode() if uids else None


def store_flags(conn, uid, keywords, sign):
    if not keywords:
        return True
    typ, _ = conn.uid("STORE", uid, f"{sign}FLAGS", f"({' '.join(keywords)})")
    return typ == "OK"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        ops = json.load(f)

    by_mailbox = defaultdict(list)
    for op in ops:
        by_mailbox[op["mailbox"]].append(op)

    conn = connect()
    done = missing = failed = 0

    try:
        for mailbox, mailbox_ops in by_mailbox.items():
            typ, _ = conn.select(f'"{mailbox}"')
            if typ != "OK":
                print(f"  SKIP mailbox not found: {mailbox} ({len(mailbox_ops)} ops)")
                failed += len(mailbox_ops)
                continue

            for op in mailbox_ops:
                try:
                    uid = find_uid(conn, op["message_id"])
                    if uid is None:
                        print(f"  NOT FOUND: {op['message_id']} in {mailbox}")
                        missing += 1
                        continue

                    ok_add = store_flags(conn, uid, op.get("add", []), "+")
                    ok_remove = store_flags(conn, uid, op.get("remove", []), "-")

                    if ok_add and ok_remove:
                        done += 1
                    else:
                        print(f"  STORE FAILED: {op['message_id']} in {mailbox}")
                        failed += 1
                except imaplib.IMAP4.error as e:
                    # One malformed header shouldn't abort the rest of the batch.
                    print(f"  ERROR: {op['message_id']} in {mailbox}: {e}")
                    failed += 1

            conn.close()
    finally:
        conn.logout()

    print(f"\nDone. {done} tagged, {missing} not found, {failed} failed.")


if __name__ == "__main__":
    main()
