#!/usr/bin/env python3
"""
Fetch a batch of not-yet-classified messages from one mailbox via plain
IMAP (no JMAP/MCP dependency, so this can run unattended on a server).

Writes two files, matching the shapes the rest of the pipeline expects:
  <prefix>_in.json  -- [{"id","subject","from","date","snippet"}, ...] for classify.py
  <prefix>_env.json -- [{"uid","internaldate","subject","from_email","message_id"}, ...]
                        for process_batch.py's Message-ID resolution

Dedup is by Message-ID against a local processed-ids file (one per line),
appended to as messages are classified (by the caller, once classification
and keyword application both succeed -- this script only reads it).

Usage:
  python3 fetch_batch.py <mailbox> <prefix> [--limit N] [--processed-ids PATH]
"""
import email
import hashlib
import imaplib
import json
import re
import sys
from email.header import decode_header
from email.utils import parseaddr

import config
from mailbox_search import search_uids

DEFAULT_LIMIT = 100
SNIPPET_MAX_CHARS = 500


def connect(timeout=60):
    username, password = config.email_address(), config.app_password()
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=timeout)
    try:
        conn.login(username, password)
    except BaseException:
        try:
            conn.shutdown()
        except Exception:
            pass
        raise
    return conn


def mailbox_uidvalidity(conn):
    _, values = conn.response('UIDVALIDITY')
    value = values[0].decode() if values and isinstance(values[0], bytes) else str(values[0]) if values else ''
    if not value.isdigit():
        raise RuntimeError('Mailbox did not report UIDVALIDITY')
    return value


def local_message_id(mailbox, uidvalidity, uid):
    identity = '\n'.join((config.email_address().lower(), mailbox, uidvalidity, uid))
    return '<tahor-uid-' + hashlib.sha256(identity.encode()).hexdigest() + '@localhost>'


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


def strip_html(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&amp;|&#\d+;|&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_body_text(raw_bytes):
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return ""

    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and html is None:
                html = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text

    body = plain if plain else html
    if body and re.search(r"<(html|div|table|body)[\s>]", body, re.IGNORECASE):
        body = strip_html(body)
    return re.sub(r"\s+", " ", body or "").strip()


def extract_snippet(raw_bytes):
    import reply_rules
    limit = 6000 if any(r['match_type'] == 'natural_language' for r in reply_rules.get_rules()) else SNIPPET_MAX_CHARS
    return extract_body_text(raw_bytes)[:limit]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    mailbox, prefix = sys.argv[1], sys.argv[2]
    limit = DEFAULT_LIMIT
    processed_path = "processed_message_ids.txt"
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
        if arg == "--processed-ids" and i + 1 < len(sys.argv):
            processed_path = sys.argv[i + 1]

    try:
        with open(processed_path) as f:
            processed = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        processed = set()

    conn = connect()
    typ, _ = conn.select(f'"{mailbox}"', readonly=True)
    if typ != "OK":
        sys.exit(f"Could not select mailbox {mailbox!r}")
    uidvalidity = mailbox_uidvalidity(conn)

    typ, data = search_uids(conn, "ALL")
    if typ != "OK":
        sys.exit("SEARCH failed")
    uids = data[0].split()
    print(f"{len(uids)} total messages in {mailbox!r}", file=sys.stderr)

    in_records, env_records = [], []
    chunk = 50
    for i in range(0, len(uids), chunk):
        if len(in_records) >= limit:
            break
        batch = uids[i : i + chunk]
        idset = b",".join(batch).decode()
        typ, fdata = conn.uid(
            "FETCH", idset,
            "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)] BODY.PEEK[])",
        )
        if typ != "OK":
            print(f"  FETCH failed for chunk starting at {i}", file=sys.stderr)
            continue

        # imaplib returns each message as two tuples (header part, body part)
        # interleaved with closing parens -- pair them up by walking in twos.
        items = [item for item in fdata if isinstance(item, tuple)]
        for j in range(0, len(items), 2):
            if len(in_records) >= limit:
                break
            meta_line, header_bytes = items[j]
            _, body_bytes = items[j + 1] if j + 1 < len(items) else (None, b"")

            uid_match = re.search(rb"UID (\d+)", meta_line)
            date_match = re.search(rb'INTERNALDATE "([^"]+)"', meta_line)
            uid = uid_match.group(1).decode() if uid_match else ""
            internaldate = date_match.group(1).decode() if date_match else ""

            header_msg = email.message_from_bytes(header_bytes)
            message_id = (header_msg.get("Message-ID") or "").strip()
            if not message_id and uid:
                message_id = local_message_id(mailbox, uidvalidity, uid)
            if not message_id or message_id in processed:
                continue

            subject = decode_str(header_msg.get("Subject", ""))
            from_raw = decode_str(header_msg.get("From", ""))
            _, from_email = parseaddr(from_raw)
            date = header_msg.get("Date", "")

            snippet = extract_snippet(body_bytes)
            import coupon_expiry
            coupon_source = {}
            if coupon_expiry.policy_for(from_email):
                full_source = subject + '\n' + extract_body_text(body_bytes)
                coupon_source['coupon_source'] = full_source[:131073]

            in_records.append(
                {"id": message_id, "subject": subject, "from": from_email, "date": date, "snippet": snippet, **coupon_source}
            )
            env_records.append(
                {
                    "uid": uid,
                    "uidvalidity": uidvalidity,
                    "internaldate": internaldate,
                    "subject": subject,
                    "from_email": from_email,
                    "message_id": message_id,
                }
            )

    conn.logout()

    with open(f"{prefix}_in.json", "w") as f:
        json.dump(in_records, f, indent=1)
    with open(f"{prefix}_env.json", "w") as f:
        json.dump(env_records, f, indent=1)

    print(f"Wrote {len(in_records)} new message(s) to {prefix}_in.json / {prefix}_env.json")


if __name__ == "__main__":
    main()
