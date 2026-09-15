#!/usr/bin/env python3
"""
Draft replies for messages from senders configured in the decision app's
reply-trigger list, without ever sending anything unattended.

For each unhandled INBOX message whose sender matches a trigger:
  1. Ask the rule model (mailbox_settings.get_rule_model()) to draft a reply.
  2. IMAP-APPEND that draft into the Drafts folder as a real in-thread reply
     (In-Reply-To/References set, \\Draft flagged) so it's editable and
     sendable from any mail client, human-in-the-loop by construction.
  3. Record it in decisions.db (reply_drafts) for the decision app's /drafts
     page, and tag the original message "draft-created" so it's skipped on
     later runs.
  4. If any drafts were created this run, send one summary email to the
     account's own address so a new draft is never silently missed.

Run this on the same schedule as the other sweeps (see README).
"""
import email
import imaplib
import json
import os
import smtplib
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import make_msgid, parseaddr

import config
import fetch_batch
import mailbox_settings
import tahor_db

MAILBOX = "INBOX"
DRAFTED_KEYWORD = "draft-created"

DRAFT_SYSTEM_PROMPT = """You draft email replies on behalf of the mailbox owner, for later human review --
your draft is never sent automatically. Write a short, direct, polite reply in the owner's
voice: plain prose, no signature block, no "Best regards" closing unless the original message's
tone calls for real formality. Reply to the substance of the message. If the message doesn't
actually need a reply (pure notification, no question or request), write "NO_REPLY_NEEDED" as
the entire response instead of a draft."""


def draft_reply_body(subject, sender, body_text):
    key = mailbox_settings.get_rule_model()
    backend = mailbox_settings.RULE_MODELS[key]
    api_key = os.environ.get(backend["auth_env"])
    if not api_key:
        raise SystemExit(f"Set {backend['auth_env']} in the environment for rule_model {backend['model']!r}.")
    payload = {
        "model": backend["model"],
        "messages": [
            {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
            {"role": "user", "content": f"From: {sender}\nSubject: {subject}\n\n{body_text[:6000]}"},
        ],
        "temperature": 0.4,
        "max_tokens": 800,
    }
    req = urllib.request.Request(
        backend["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["choices"][0]["message"]["content"].strip()


def append_draft(conn, in_reply_to, references, to_addr, subject, body_text):
    from_addr = config.email_address()
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg = MIMEText(body_text)
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = str(Header(reply_subject, "utf-8"))
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = f"{references} {in_reply_to}".strip()
    msg["Message-ID"] = make_msgid()
    conn.append("Drafts", "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())


def notify(created):
    if not created:
        return
    lines = [f"- Re: {c['subject']} (to {c['to']})" for c in created]
    body = (
        f"{len(created)} new reply draft(s) waiting for review in Drafts and in the decision app:\n\n"
        + "\n".join(lines)
        + f"\n\n{os.environ.get('BASE_URL', '')}/drafts"
    )
    msg = MIMEText(body)
    msg["From"] = config.email_address()
    msg["To"] = config.email_address()
    msg["Subject"] = f"Tahor: {len(created)} new reply draft(s) waiting"
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as smtp:
        smtp.login(config.email_address(), config.app_password())
        smtp.send_message(msg)


def main():
    triggers = mailbox_settings.get_reply_triggers()
    if not triggers:
        print("No reply triggers configured, nothing to do.")
        return

    conn = fetch_batch.connect()
    conn.select(f'"{MAILBOX}"')

    uids = set()
    for t in triggers:
        needle = t["value"] if t["type"] == "sender_email" else f"@{t['value']}"
        typ, data = conn.search(None, "UNKEYWORD", DRAFTED_KEYWORD, "FROM", f'"{needle}"')
        if typ == "OK":
            uids.update(data[0].split())

    created = []
    for uid in sorted(uids, key=int):
        typ, fdata = conn.fetch(uid, "(BODY.PEEK[])")
        if typ != "OK" or not fdata or not isinstance(fdata[0], tuple):
            continue
        raw = fdata[0][1]
        msg = email.message_from_bytes(raw)
        _, sender_email = parseaddr(msg.get("From", ""))
        if not mailbox_settings.matches_reply_trigger(sender_email):
            continue

        message_id = (msg.get("Message-ID") or "").strip()
        if not message_id:
            continue
        subject = fetch_batch.decode_str(msg.get("Subject", ""))
        references = msg.get("References", "")
        body_text = fetch_batch.extract_body_text(raw)

        try:
            draft_body = draft_reply_body(subject, sender_email, body_text)
        except Exception as e:
            print(f"  {message_id}: draft failed: {e}")
            continue

        conn.store(uid, "+FLAGS", f"({DRAFTED_KEYWORD})")
        if draft_body.strip() == "NO_REPLY_NEEDED":
            print(f"  {message_id}: no reply needed, skipped")
            continue

        append_draft(conn, message_id, references, sender_email, subject, draft_body)
        tahor_db.create_reply_draft(message_id, sender_email, subject, draft_body, trigger_reason=sender_email)
        created.append({"subject": subject, "to": sender_email})
        print(f"  drafted reply to {sender_email}: {subject}")

    conn.logout()
    notify(created)
    print(f"Done. {len(created)} draft(s) created.")


if __name__ == "__main__":
    main()
