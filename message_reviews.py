"""Read and identify one review message without marking it read or exposing raw MIME."""
import email
import json
import hashlib
import time
import re
from datetime import datetime, timezone

import fetch_batch
from mailbox_paths import quote_mailbox, list_mailboxes


class LookupPending(RuntimeError):
    """A bounded mailbox search will resume using progress saved in context."""


def _folder_matches(client, mailbox, identifier, saved=None):
    if client.select(quote_mailbox(mailbox), readonly=True)[0] != 'OK':
        raise RuntimeError('A folder is unavailable; the message search will retry')
    validity = fetch_batch.mailbox_uidvalidity(client)
    if saved and saved.get('uid') and str(saved.get('uidvalidity')) == validity:
        candidates = [str(saved['uid']).encode('ascii')]
    else:
        escaped = identifier.replace('\\', '\\\\').replace('"', '\\"')
        status, rows = client.uid('SEARCH', None, 'HEADER', 'Message-ID', '"' + escaped + '"')
        if status != 'OK':
            raise RuntimeError('The message search will retry when the mailbox is available')
        candidates = rows[0].split() if rows and rows[0] else []
    if len(candidates) > 20:
        raise RuntimeError('Multiple messages match this decision; review the message in your mailbox')
    matches = []
    for uid in candidates:
        if not uid.isdigit():
            raise RuntimeError('The mailbox returned an invalid message identity')
        status, rows = client.uid('FETCH', uid, '(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)]<0.65537>)')
        items = [row for row in (rows or []) if isinstance(row, tuple)]
        if status != 'OK':
            raise RuntimeError('Message details could not be read; the decision remains pending')
        if not items:
            continue
        if len(items) != 1:
            raise RuntimeError('Message identity could not be verified')
        metadata, raw = items[0]
        actual = re.search(rb'\bUID (\d+)\b', metadata)
        delivered = re.search(rb'\bINTERNALDATE "([^"]+)"', metadata)
        if not actual or actual[1] != uid or not delivered or not isinstance(raw, bytes) or len(raw) > 65536:
            raise RuntimeError('Message identity could not be verified')
        message = email.message_from_bytes(raw)
        ids = message.get_all('Message-ID', [])
        exact = len(ids) == 1 and str(ids[0]).strip() == identifier
        local = not ids and identifier == fetch_batch.local_message_id(mailbox, validity, uid.decode())
        if not exact and not local:
            continue
        arrival = datetime.strptime(delivered[1].decode('ascii'), '%d-%b-%Y %H:%M:%S %z').astimezone(timezone.utc).isoformat()
        matches.append(dict(mailbox=mailbox, uid=uid.decode(), uidvalidity=validity,
                            subject=fetch_batch.decode_str(message.get('Subject', ''))[:500],
                            sender=fetch_batch.decode_str(message.get('From', ''))[:500],
                            date=fetch_batch.decode_str(message.get('Date', ''))[:200],
                            received_at=arrival))
    return matches


class _DeadlineMailbox:
    """Apply one lookup deadline to every network command, including folder scans."""
    def __init__(self, client, deadline):
        self.client, self.deadline = client, deadline

    def __getattr__(self, name):
        value = getattr(self.client, name)
        if not callable(value):
            return value
        def bounded(*args, **kwargs):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise LookupPending('Message details will continue loading in the next background pass.')
            self.client.sock.settimeout(max(.1, min(5, remaining)))
            return value(*args, **kwargs)
        return bounded

    def logout(self):
        try:
            self.client.sock.settimeout(.5)
            self.client.logout()
        except Exception:
            try:
                self.client.shutdown()
            except Exception:
                pass


def locate(context, budget_seconds=20, bounded=False):
    """Verify the original location, then resume a bounded exact-ID folder search.

    Search progress lives in the private review context; no partial search can
    authorize a mutation. The caller must persist context on LookupPending.
    """
    mailbox, identifier = context['mailbox'], context['message_id']
    if not isinstance(identifier, str) or not identifier or any(ord(c) < 32 for c in identifier):
        raise ValueError('Message identity needs repair before this decision can be applied')
    deadline = time.monotonic() + budget_seconds
    client = fetch_batch.connect(timeout=max(.1, min(5, budget_seconds / 2))) if bounded else fetch_batch.connect()
    if bounded:
        client = _DeadlineMailbox(client, deadline)
    try:
        original = _folder_matches(client, mailbox, identifier, context)
        if not original and context.get('uid'):
            original = _folder_matches(client, mailbox, identifier)
            if any(str(item['uidvalidity']) == str(context.get('uidvalidity'))
                   and str(item['uid']) != str(context['uid']) for item in original):
                raise RuntimeError('A different copy of this message exists in the original folder; no other message was selected')
        if len(original) == 1:
            context.pop('review_search', None)
            return original[0]
        if len(original) > 1:
            raise RuntimeError('Multiple messages match this decision; no changes were made')
        folders = sorted(name for name, _ in list_mailboxes(client) if name != mailbox)
        identity = hashlib.sha256(json.dumps(folders).encode()).hexdigest()
        progress = context.get('review_search')
        if not isinstance(progress, dict) or progress.get('folders') != identity:
            progress = context['review_search'] = {'folders': identity, 'next_index': 0, 'matches': []}
        while progress['next_index'] < len(folders):
            if time.monotonic() >= deadline:
                raise LookupPending('Searching other folders for the moved message. Your choice is saved and will retry automatically.')
            folder = folders[progress['next_index']]
            matches = _folder_matches(client, folder, identifier)
            progress['matches'].extend(matches)
            progress['next_index'] += 1
            if len(progress['matches']) > 1:
                raise RuntimeError('Multiple messages match this decision; no changes were made')
        matches = progress['matches']
        context.pop('review_search', None)
        if len(matches) != 1:
            raise RuntimeError('The original message could not be found. Your choice is saved for retry; no other message was changed.')
        # A saved result could have moved while subsequent folders were scanned.
        verified = _folder_matches(client, matches[0]['mailbox'], identifier, matches[0])
        if len(verified) != 1:
            raise LookupPending('The message moved during the search. Your saved choice will retry automatically.')
        return verified[0]
    finally:
        client.logout()


def refresh(decision_id, database, budget_seconds=None):
    """Enrich a pending card only if its context has not changed during the lookup."""
    row = database.execute("SELECT * FROM decisions WHERE id=? AND kind='message_review' AND status='pending'", (decision_id,)).fetchone()
    if row is None:
        raise ValueError('This message no longer needs review')
    context = json.loads(row['context'] or '{}')
    try:
        details = locate(context) if budget_seconds is None else locate(context, budget_seconds=budget_seconds, bounded=True)
    except Exception:
        with database:
            database.execute("UPDATE decisions SET context=? WHERE id=? AND context=? AND status='pending'", (json.dumps(context), decision_id, row['context']))
        raise
    updated = dict(context, **details)
    updated["details_loaded"] = True
    with database:
        changed = database.execute("UPDATE decisions SET summary=?, context=? WHERE id=? AND context=? AND status='pending'", (details['subject'], json.dumps(updated), decision_id, row['context']))
    if changed.rowcount != 1:
        raise RuntimeError('The decision changed while its details were loading; refresh the page')
    return updated


def read_message(context, max_bytes=1024 * 1024, budget_seconds=12):
    """Render text only; never load remote content or mark the message read."""
    deadline = time.monotonic() + budget_seconds
    details = locate(context, budget_seconds=min(8, budget_seconds), bounded=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LookupPending('Message details will continue loading when you retry the preview.')
    client = _DeadlineMailbox(fetch_batch.connect(timeout=max(.1, min(3, remaining / 2))), deadline)
    try:
        if client.select(quote_mailbox(details['mailbox']), readonly=True)[0] != 'OK':
            raise RuntimeError('The message folder is unavailable')
        if fetch_batch.mailbox_uidvalidity(client) != details['uidvalidity']:
            raise RuntimeError('The message folder changed; refresh its details')
        status, rows = client.uid('FETCH', details['uid'], f'(UID BODY.PEEK[]<0.{max_bytes + 1}>)')
        items = [item for item in (rows or []) if isinstance(item, tuple)]
        if status != 'OK' or len(items) != 1:
            raise RuntimeError('The message body could not be read')
        metadata, raw = items[0]
        uid = re.search(rb'\bUID (\d+)\b', metadata)
        if not uid or uid[1].decode() != details['uid'] or not isinstance(raw, bytes):
            raise RuntimeError('The message identity changed; refresh its details')
        if len(raw) > max_bytes:
            return details, 'This message exceeds the 1 MiB viewer limit. Open it in your mail client to read the complete message and attachments.'
        message = email.message_from_bytes(raw)
        ids = message.get_all('Message-ID', [])
        identifier = context['message_id']
        exact = len(ids) == 1 and str(ids[0]).strip() == identifier
        local = not ids and identifier == fetch_batch.local_message_id(details['mailbox'], details['uidvalidity'], details['uid'])
        if not exact and not local:
            raise RuntimeError('The message identity changed; refresh its details')
        return details, fetch_batch.extract_body_text(raw) or 'No readable text body. Open this message in your mail client to review its attachments.'
    finally:
        client.logout()


def hydrate_pending(database, limit=3, budget_seconds=30):
    """Fair, bounded background enrichment; missing mail never blocks later cards."""
    with database:
        database.execute('CREATE TABLE IF NOT EXISTS message_review_refresh (decision_id INTEGER PRIMARY KEY, attempted_at REAL NOT NULL, retry_after REAL NOT NULL)')
    now = time.time()
    candidates = database.execute("SELECT d.* FROM decisions d LEFT JOIN message_review_refresh r ON r.decision_id=d.id WHERE d.kind='message_review' AND d.status='pending' AND (r.retry_after IS NULL OR r.retry_after<=?) ORDER BY COALESCE(r.attempted_at,0),d.id", (now,)).fetchall()
    deadline = time.monotonic() + max(1, budget_seconds)
    attempted = updated = 0
    for row in candidates:
        if attempted >= max(1, min(int(limit), 20)) or time.monotonic() >= deadline:
            break
        try:
            context = json.loads(row['context'] or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(context, dict) or not context.get('mailbox') or not context.get('message_id'):
            continue
        if context.get('details_loaded') or (context.get('subject') and context.get('sender') and (context.get('received_at') or context.get('date'))):
            continue
        # Claim before network I/O; competing workers cannot start the same lookup.
        with database:
            claim = database.execute('INSERT INTO message_review_refresh(decision_id,attempted_at,retry_after) VALUES(?,?,?) ON CONFLICT(decision_id) DO UPDATE SET attempted_at=excluded.attempted_at,retry_after=excluded.retry_after WHERE message_review_refresh.retry_after<=?', (row['id'], now, now + 300, now))
        if claim.rowcount != 1:
            continue
        attempted += 1
        try:
            refresh(row['id'], database, budget_seconds=max(.1, min(10, deadline - time.monotonic())))
            updated += 1
        except Exception:
            # Search cursors are saved by refresh. The next card still gets a turn.
            pass
    return {'attempted': attempted, 'updated': updated}
