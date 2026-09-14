#!/usr/bin/env python3
"""
Fetch Message-ID/Subject/From/Date headers for every message in a mailbox
within a date window, in one IMAP round trip.

Usage:
  python3 bulk_lookup.py <mailbox> <since YYYY-MM-DD> <before YYYY-MM-DD> <output.json>

IMAP SINCE/BEFORE are whole-day, server-timezone granularity (SINCE
inclusive, BEFORE exclusive) — pad the window a day on each side of what
you actually need, then correlate downstream by subject + from-email.

Output: JSON array of
  {"uid": "123", "internaldate": "2021-03-19T05:12:30", "subject": "...",
   "from_email": "a@b.com", "message_id": "<...>"}
"""
import datetime
import email
import imaplib
import json
import sys
import time
from email.header import decode_header
from email.utils import parseaddr

import config


def connect():
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    conn.login(config.email_address(), config.app_password())
    return conn


def decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def to_imap_date(iso_date):
    return datetime.date.fromisoformat(iso_date).strftime("%d-%b-%Y")


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(1)
    mailbox, since, before, out_path = sys.argv[1:5]

    conn = connect()
    try:
        typ, _ = conn.select(f'"{mailbox}"', readonly=True)
        if typ != "OK":
            sys.exit(f"Could not select mailbox {mailbox!r}")

        typ, data = conn.search(None, f"(SINCE {to_imap_date(since)} BEFORE {to_imap_date(before)})")
        if typ != "OK":
            sys.exit("SEARCH failed")
        uids = data[0].split()
        if not uids:
            json.dump([], open(out_path, "w"))
            print("No messages in range.", file=sys.stderr)
            return

        results = []
        chunk = 200
        for i in range(0, len(uids), chunk):
            batch = uids[i : i + chunk]
            idset = b",".join(batch).decode()
            typ, fdata = conn.fetch(
                idset, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"
            )
            if typ != "OK":
                print(f"  FETCH failed for chunk starting at {i}", file=sys.stderr)
                continue
            for item in fdata:
                if not isinstance(item, tuple):
                    continue
                meta_line, header_bytes = item
                meta_line = meta_line.decode("utf-8", errors="replace")
                uid_str = meta_line.split()[0]
                internaldate = None
                if 'INTERNALDATE "' in meta_line:
                    idate_raw = meta_line.split('INTERNALDATE "')[1].split('"')[0]
                    try:
                        dt = imaplib.Internaldate2tuple(f'"{idate_raw}"'.encode())
                        internaldate = time.strftime("%Y-%m-%dT%H:%M:%S", dt) if dt else idate_raw
                    except Exception:
                        internaldate = idate_raw
                msg = email.message_from_bytes(header_bytes)
                subject = decode_str(msg.get("Subject", ""))
                _, from_email = parseaddr(decode_str(msg.get("From", "")))
                results.append(
                    {
                        "uid": uid_str,
                        "internaldate": internaldate,
                        "subject": subject,
                        "from_email": from_email.lower(),
                        "message_id": (msg.get("Message-ID", "") or "").strip(),
                    }
                )
            print(f"  fetched {min(i + chunk, len(uids))}/{len(uids)}", file=sys.stderr)

        with open(out_path, "w") as f:
            json.dump(results, f, indent=1)
        print(f"Done. {len(results)} envelopes written to {out_path}", file=sys.stderr)
    finally:
        conn.logout()


if __name__ == "__main__":
    main()
