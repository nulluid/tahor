"""Enrich legacy filing requests from bounded, read-only mailbox samples."""
import email
from email import policy
import json
import re
import time

import config
import fetch_batch
from mailbox_paths import quote_mailbox
from mailbox_search import search_uids
import tahor_db


def enrich_pending(limit=3):
    conn = tahor_db.get_db()
    mailbox = None
    failures = 0
    try:
        rows = []
        for row in conn.execute("SELECT id,context FROM decisions WHERE kind='vendor_mapping' AND status='pending' AND resolution IS NULL").fetchall():
            try:
                context = json.loads(row['context'] or '{}')
            except (ValueError, TypeError):
                continue
            if not isinstance(context, dict) or context.get('routing_key'):
                continue
            label = context.get('sender_label')
            if not isinstance(label, str) or not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', label):
                continue
            checked = context.get('inventory_checked_at', 0)
            rows.append((checked if type(checked) in (int, float) else 0, row, context))
        rows.sort(key=lambda item: (item[0], item[1]['id']))
        for _, row, context in rows[:min(10, max(0, int(limit)))]:
            updated = dict(context, inventory_checked_at=time.time(), inventory_status='checking')
            with conn:
                claimed = conn.execute("UPDATE decisions SET context=? WHERE id=? AND context=? AND status='pending' AND resolution IS NULL",
                                       (json.dumps(updated), row['id'], row['context'])).rowcount
            if not claimed:
                continue
            try:
                if mailbox is None:
                    mailbox = fetch_batch.connect()
                base = config.filing_root() + '/_Unsorted/' + context['sender_label']
                # Both current filing destinations and the old unsorted root can
                # hold evidence. Inbox is a fallback for mail not filed yet.
                folders = [base, base + '/Receipts', base + '/Correspondence', 'INBOX']
                found = 0
                existing = False
                for folder in folders:
                    if folder == 'INBOX' and found:
                        break
                    status, listing = mailbox.list('""', quote_mailbox(folder))
                    if status != 'OK':
                        raise RuntimeError('Legacy filing folder lookup failed')
                    if not listing or not any(listing):
                        continue
                    existing = True
                    if mailbox.select(quote_mailbox(folder), readonly=True)[0] != 'OK':
                        raise RuntimeError('Legacy filing samples are unavailable')
                    validity = fetch_batch.mailbox_uidvalidity(mailbox)
                    # Filing includes correspondence, not just receipts; older
                    # messages may have no classification keywords at all.
                    criteria = ('FROM', '"@' + context['sender_label'] + '"') if folder == 'INBOX' else ('ALL',)
                    status, data = search_uids(mailbox, *criteria)
                    if status != 'OK':
                        raise RuntimeError('Legacy filing sample search failed')
                    uids = sorted(data[0].split() if data and data[0] else [], key=int)[-25:]
                    for uid in uids:
                        status, values = mailbox.uid('FETCH', uid, '(UID INTERNALDATE BODY.PEEK[]<0.8192>)')
                        parts = [item for item in (values or []) if isinstance(item, tuple)]
                        if status != 'OK' or len(parts) != 1 or not isinstance(parts[0][1], bytes) or len(parts[0][1]) > 8192:
                            raise RuntimeError('Legacy filing sample could not be verified')
                        match = re.search(rb'\bUID (\d+)\b', parts[0][0])
                        if not match or match[1] != uid:
                            raise RuntimeError('Legacy sample identity changed')
                        message = email.message_from_bytes(parts[0][1], policy=policy.default)
                        if len(message.get_all('From', [])) != 1 or len(message.get_all('Message-ID', [])) != 1:
                            continue
                        addresses = email.utils.getaddresses([str(message['From'])])
                        if len(addresses) != 1:
                            continue
                        display, sender = addresses[0]
                        if not re.fullmatch(r'[^\s<>@"\\]+@[a-zA-Z0-9.-]+', sender):
                            continue
                        domain = sender.rsplit('@', 1)[1].lower()
                        expected = context['sender_label'].lower()
                        if domain != expected and not domain.endswith('.' + expected):
                            continue
                        delivered = re.search(rb'\bINTERNALDATE "([^"]+)"', parts[0][0])
                        tahor_db.queue_vendor_mapping(context['sender_label'], metadata={
                            'sender_email': sender.lower(), 'display_name': display,
                            'subject': str(message.get('Subject', '')), 'date': str(message.get('Date', '')),
                            'received_at': delivered[1].decode('ascii', errors='replace') if delivered else '',
                            'mailbox': folder, 'message_id': str(message['Message-ID']),
                            'uid': uid.decode('ascii'), 'uidvalidity': validity,
                            'excerpt': fetch_batch.extract_body_text(parts[0][1])[:500],
                            'suggested_vendor': display,
                        })
                        found += 1
                with conn:
                    empty = dict(updated, inventory_status='enriched' if found else ('no_samples' if existing else 'missing_folder'))
                    conn.execute("UPDATE decisions SET context=? WHERE id=? AND context=? AND status='pending' AND resolution IS NULL",
                                 (json.dumps(empty), row['id'], json.dumps(updated)))
            except Exception:
                failures += 1
                if mailbox is not None:
                    try:
                        mailbox.logout()
                    except Exception:
                        pass
                    mailbox = None
    finally:
        conn.close()
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception:
                pass
    return failures
