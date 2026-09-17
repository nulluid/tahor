"""Bounded, read-only discovery of messages behind a subscription card."""
import email
from email.utils import getaddresses
import imaplib
import json
import re
import time
from datetime import datetime, timezone

import config
import fetch_batch
from mailbox_paths import list_mailboxes, quote_mailbox


def scan(database, candidate_id, *, folder_limit=2, header_limit=20, budget_seconds=8):
    """Search only on request, persisting progress between small batches."""
    import tahor_db
    row = database.execute('SELECT * FROM unsubscribe_candidates WHERE id=?', (candidate_id,)).fetchone()
    if row is None:
        raise ValueError('This subscription no longer exists')
    sender = (row['sender_email'] or '').strip().lower()
    if not re.fullmatch(r'[^\s<>"\\@]+@[^\s<>"\\@]+', sender) or any(ord(c) < 32 for c in sender):
        raise ValueError('This subscription has no exact sender address to search safely')
    with database:
        database.execute('CREATE TABLE IF NOT EXISTS subscription_message_search (candidate_id INTEGER PRIMARY KEY, state_json TEXT NOT NULL)')
    saved = database.execute('SELECT state_json FROM subscription_message_search WHERE candidate_id=?', (candidate_id,)).fetchone()
    state = json.loads(saved[0]) if saved else {}
    if state.get('sender') != sender:
        state = {'sender': sender, 'next': 0}
    if state.get('complete'):
        return state
    deadline = time.monotonic() + budget_seconds
    client = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=min(8, budget_seconds))
    def bounded():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('The bounded search can continue on the next request')
        client.sock.settimeout(min(5, remaining))
    try:
        bounded()
        client.login(config.email_address(), config.app_password())
        bounded()
        folders = ['INBOX'] + sorted(name for name, _ in list_mailboxes(client) if name.upper() != 'INBOX')
        if state.get('folders') != folders:
            state.update(folders=folders, next=0)
        matches = 0
        for _ in range(folder_limit):
            if state['next'] >= len(folders) or time.monotonic() >= deadline:
                break
            bounded()
            mailbox = folders[state['next']]
            if client.select(quote_mailbox(mailbox), readonly=True)[0] != 'OK':
                raise RuntimeError('A folder is temporarily unavailable. Retry the search.')
            validity = fetch_batch.mailbox_uidvalidity(client)
            bounded()
            status, values = client.uid('SEARCH', None, 'FROM', '"' + sender + '"')
            if status != 'OK':
                raise RuntimeError('Message search is temporarily unavailable')
            uids = values[0].split() if values and values[0] else []
            if any(not uid.isdigit() for uid in uids):
                raise RuntimeError('The mailbox returned an invalid identity')
            recent = state.get('remaining')
            if recent is None:
                recent = [uid.decode() for uid in sorted(uids, key=int, reverse=True)[:header_limit]]
            recent = [uid.encode() for uid in recent]
            state['remaining'] = [uid.decode() for uid in recent]
            for uid in recent:
                if time.monotonic() >= deadline:
                    break
                bounded()
                state['remaining'].remove(uid.decode())
                status, values = client.uid('FETCH', uid, '(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)]<0.65537>)')
                parts = [item for item in (values or []) if isinstance(item, tuple)]
                if status != 'OK' or len(parts) != 1:
                    continue
                metadata, raw = parts[0]
                actual = re.search(rb'\bUID (\d+)\b', metadata)
                arrival = re.search(rb'\bINTERNALDATE "([^"]+)"', metadata)
                if not actual or actual[1] != uid or not arrival or not isinstance(raw, bytes) or len(raw) > 65536:
                    continue
                message = email.message_from_bytes(raw)
                addresses = getaddresses(message.get_all('From', []))
                if len(addresses) != 1 or addresses[0][1].lower() != sender:
                    continue
                identifiers = message.get_all('Message-ID', [])
                if len(identifiers) > 1:
                    continue
                identifier = str(identifiers[0]).strip() if identifiers else fetch_batch.local_message_id(mailbox, validity, uid.decode())
                if not identifier or any(ord(c) < 32 for c in identifier):
                    continue
                received = datetime.strptime(arrival[1].decode('ascii'), '%d-%b-%Y %H:%M:%S %z').astimezone(timezone.utc).isoformat()
                tahor_db.record_subscription_sample(candidate_id, dict(mailbox=mailbox, message_id=identifier, uid=uid.decode(), uidvalidity=validity,
                    sender_email=sender, display_name=fetch_batch.decode_str(addresses[0][0]), subject=fetch_batch.decode_str(message.get('Subject', ''))[:500],
                    date=fetch_batch.decode_str(message.get('Date', ''))[:200], received_at=received))
                matches += 1
                if matches >= 3:
                    break
            if not state['remaining']:
                state['next'] += 1
                state.pop('remaining', None)
            if matches >= 3:
                break
        state['complete'] = state['next'] >= len(folders)
        with database:
            database.execute('INSERT OR REPLACE INTO subscription_message_search(candidate_id,state_json) VALUES(?,?)', (candidate_id, json.dumps(state)))
        return state
    finally:
        with database:
            database.execute('INSERT OR REPLACE INTO subscription_message_search(candidate_id,state_json) VALUES(?,?)', (candidate_id, json.dumps(state)))
        try:
            client.sock.settimeout(.5)
            client.logout()
        except Exception:
            try:
                client.shutdown()
            except Exception:
                pass
