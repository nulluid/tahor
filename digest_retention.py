"""Retire only mailbox digests whose identity and content match our private journal."""
from datetime import datetime, timezone
import email
from email import policy
import json

import config
import mailbox_settings
from mailbox_paths import quote_mailbox
from mailbox_search import search_uids
import notifications
from message_expiry import metadata as _metadata, expired as _expired

KEYWORD = 'category-tahor-digest'
MAX_MESSAGE_BYTES = 64 * 1024


def known_digests():
    path = notifications.state_path()
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or not isinstance(state.get('events'), dict):
        raise ValueError('Notification journal needs repair before digest cleanup')
    return {notifications.notification_message_id(key): value
            for key, value in state['events'].items()
            if key.startswith('digest:') and isinstance(value, dict)
            and value.get('kind') == 'digest' and value.get('status') == 'sent'
            and isinstance(value.get('body'), str) and isinstance(value.get('subject'), str)}


def _body(value):
    return value.replace('\r\n', '\n').rstrip('\n')


def sweep(conn, path, records, dry_run, delete_uids, now=None):
    if not records:
        return 0, 0
    if conn.select(quote_mailbox(path), readonly=dry_run)[0] != 'OK':
        raise RuntimeError('Digest cleanup could not select a mailbox')
    status, rows = search_uids(conn, 'HEADER', 'X-Tahor-Notification', '"1"',
                               'SMALLER', str(MAX_MESSAGE_BYTES))
    if status != 'OK':
        raise RuntimeError('Digest cleanup search failed; no messages changed')
    now = now or datetime.now(timezone.utc)
    grace = mailbox_settings.get_inbox_grace_days()
    found = deleted = 0
    for uid in rows[0].split() if rows and rows[0] else []:
        status, items = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE BODY.PEEK[])')
        if status != 'OK':
            raise RuntimeError('Digest message could not be read')
        _, flags, delivered = _metadata(items, uid, content=True)
        raw = next(row[1] for row in items if isinstance(row, tuple))
        if not isinstance(raw, bytes) or len(raw) >= MAX_MESSAGE_BYTES:
            continue
        message = email.message_from_bytes(raw, policy=policy.default)
        identifiers = message.get_all('Message-ID', [])
        if len(identifiers) != 1 or str(identifiers[0]) not in records:
            continue
        record = records[str(identifiers[0])]
        expected = {'From': config.email_address(), 'To': config.email_address(),
                    'Subject': record['subject'], 'X-Tahor-Notification': '1',
                    'Auto-Submitted': 'auto-generated'}
        if any([str(value) for value in message.get_all(name, [])] != [value]
               for name, value in expected.items()):
            continue
        if message.is_multipart() or message.get_content_type() != 'text/plain':
            continue
        if _body(message.get_content()) != _body(record['body']):
            continue
        # Reconcile earlier journaled digests as well as newly labeled notices.
        if not dry_run and KEYWORD.encode() not in flags:
            if conn.uid('STORE', uid, '+FLAGS', '(category-notification '+KEYWORD+' retention-standard)')[0] != 'OK':
                raise RuntimeError('Digest classification could not be saved')
        if not _expired(flags, delivered, now, grace):
            continue
        # An owner may have marked it unread or protected it during this sweep.
        status, items = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE)')
        if status != 'OK':
            raise RuntimeError('Digest read state could not be rechecked')
        _, current_flags, current_date = _metadata(items, uid)
        if current_date != delivered or not _expired(current_flags, current_date, now, grace):
            continue
        matched, removed = delete_uids(conn, [uid], dry_run)
        found += matched
        deleted += removed
    return found, deleted
