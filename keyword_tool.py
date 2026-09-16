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
    "uid": "1234",  (optional -- if present, skips the Message-ID SEARCH lookup)
    "add": ["receipt", "retention-forever"],
    "remove": ["unclassified"]
  }
"""
import imaplib
import json
import re
import sys
from collections import defaultdict

import config


def connect():
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=60)
    conn.login(config.email_address(), config.app_password())
    return conn


def find_uid(conn, message_id):
    # Some real-world messages have malformed headers where Message-ID gets
    # concatenated with a fragment of the next header line during parsing,
    # embedding a literal \r\n -- imaplib's own client-side validation
    # rejects control characters in commands outright (ValueError, not
    # IMAP4.error), so strip them rather than let one bad header abort
    # the whole batch.
    message_id = "".join(c for c in message_id if ord(c) >= 32)
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
    if sign not in ("+", "-") or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", keyword) for keyword in keywords):
        raise ValueError("Invalid IMAP keyword operation")
    typ, _ = conn.uid("STORE", uid, f"{sign}FLAGS", f"({' '.join(keywords)})")
    return typ == "OK"


def apply_ops(ops):
    by_mailbox = defaultdict(list)
    for op in ops:
        by_mailbox[op["mailbox"]].append(op)
    outcome = {"applied": set(), "failed": set(), "missing": set()}
    if not ops:
        return outcome
    conn = connect()
    try:
        for mailbox, mailbox_ops in by_mailbox.items():
            typ, _ = conn.select('"' + mailbox.replace('\\', '\\\\').replace('"', '\\"') + '"')
            if typ != "OK":
                outcome["failed"].update(op["message_id"] for op in mailbox_ops)
                continue
            for op in mailbox_ops:
                message_id = op["message_id"]
                try:
                    uid = op.get("uid") or find_uid(conn, message_id)
                    if uid is None:
                        outcome["missing"].add(message_id)
                        continue
                    # Verify UID shortcuts still refer to a message. STORE can
                    # return OK for a UID removed concurrently by another client.
                    typ, data = conn.uid("FETCH", uid, "(UID)")
                    if typ != "OK" or not data or not any(item for item in data if item is not None):
                        outcome["missing"].add(message_id)
                        continue
                    ok_add = store_flags(conn, uid, op.get("add", []), "+")
                    ok_remove = store_flags(conn, uid, op.get("remove", []), "-") if ok_add else False
                    outcome["applied" if ok_add and ok_remove else "failed"].add(message_id)
                except (imaplib.IMAP4.error, ValueError, OSError):
                    outcome["failed"].add(message_id)
            # CLOSE expunges unrelated messages already marked Deleted.
            if hasattr(conn, "unselect"):
                conn.unselect()
    finally:
        conn.logout()
    print(f"Done. {len(outcome['applied'])} tagged, {len(outcome['missing'])} not found, {len(outcome['failed'])} failed.")
    return outcome


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    with open(sys.argv[1]) as source:
        outcome = apply_ops(json.load(source))
    if outcome["failed"] or outcome["missing"]:
        raise SystemExit(1)
    return outcome


if __name__ == "__main__":
    main()
