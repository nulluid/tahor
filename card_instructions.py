"""Private owner instructions attached to cards; saves never execute mailbox work."""
from datetime import datetime, timezone
from email.utils import parseaddr
import hashlib
import json
import re
import uuid


SCHEMA = '''CREATE TABLE IF NOT EXISTS card_instructions (
    source_kind TEXT NOT NULL,source_id INTEGER NOT NULL,sender_email TEXT NOT NULL,
    instructions TEXT NOT NULL,revision TEXT NOT NULL,updated_at TEXT NOT NULL,
    proposal_decision_id INTEGER,PRIMARY KEY(source_kind,source_id))'''


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_instructions'").fetchone() is not None


def _address(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return ''
    address = parseaddr(value)[1].strip().lower()
    return address if len(address) <= 320 and re.fullmatch(r'[^\s<>"\\@]+@[a-z0-9.-]+', address) else ''


def get_all_card_instructions(kind):
    if kind not in ('subscription', 'decision'):
        raise ValueError('Unknown instruction card type.')
    import tahor_db
    conn = tahor_db.get_db()
    try:
        if not _exists(conn):
            return {}
        return {row['source_id']: row['instructions'] for row in conn.execute('SELECT source_id,instructions FROM card_instructions WHERE source_kind=?', (kind,))}
    finally:
        conn.close()


def get_card_instructions(kind, identifier):
    return get_all_card_instructions(kind).get(identifier, '')


def revision(conn):
    if not _exists(conn):
        return ''
    rows = [tuple(row) for row in conn.execute('SELECT source_kind,source_id,revision FROM card_instructions ORDER BY source_kind,source_id')]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def for_senders(conn, senders):
    if not _exists(conn):
        return []
    wanted = {_address(sender) for sender in senders} - {''}
    result, used, total = [], set(), 0
    for row in conn.execute('SELECT sender_email,instructions FROM card_instructions ORDER BY updated_at DESC,source_id DESC'):
        sender = row['sender_email']
        if sender not in wanted or sender in used:
            continue
        item = {'sender_email': sender, 'instructions': row['instructions']}
        size = len(json.dumps(item).encode())
        if total + size > 16000:
            break
        result.append(item); used.add(sender); total += size
        if len(used) == len(wanted):
            break
    return result


def validate_proposal(context, proposal):
    source = context.get('card_instruction_source')
    if source is not None:
        import tahor_db
        conn = tahor_db.get_db()
        try:
            current = (conn.execute('SELECT revision FROM card_instructions WHERE source_kind=? AND source_id=?',
                       (source.get('kind'), source.get('id'))).fetchone()
                       if isinstance(source, dict) and _exists(conn) else None)
            if current is None or current['revision'] != context.get('card_instruction_revision'):
                raise ValueError('These card instructions changed. Generate and review a new proposal.')
        finally:
            conn.close()
    if context.get('card_instruction_exact_sender') and proposal.get('kind') == 'sender_rule':
        raise ValueError('This card instruction addresses one sender; a domain-wide sender block needs a separate explicit rule.')


def save_card_instructions(kind, identifier, text, propose_rule=False):
    if kind not in ('subscription', 'decision') or type(identifier) is not int or identifier < 1:
        raise ValueError('Invalid instruction card.')
    if (not isinstance(text, str) or not text.strip() or len(text) > 4000
            or any(ord(c) < 32 and c not in '\n\t' for c in text)):
        raise ValueError('Enter instructions of at most 4,000 characters.')
    if type(propose_rule) is not bool:
        raise ValueError('Invalid instruction action.')
    text = text.strip()
    import tahor_db
    conn = tahor_db.get_db()
    try:
        with conn:
            conn.execute(SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            table = 'unsubscribe_candidates' if kind == 'subscription' else 'decisions'
            source = conn.execute('SELECT * FROM ' + table + ' WHERE id=?', (identifier,)).fetchone()
            if source is None:
                raise LookupError('The instruction card no longer exists.')
            if source['status'] != 'pending':
                raise ValueError('This card was already handled. Reload its current state.')
            context = {}
            if kind == 'subscription':
                sender = _address(source['sender_email'])
                if not sender:
                    raise ValueError('An exact sender address is required for subscription guidance.')
            else:
                if source['kind'] not in ('vendor_mapping', 'message_review', 'free_text_rule'):
                    raise ValueError('This card does not support contextual instructions.')
                if source['resolution'] and source['kind'] != 'free_text_rule':
                    raise ValueError('This card already has a saved choice being processed.')
                try:
                    context = json.loads(source['context'] or '{}')
                except (TypeError, ValueError):
                    context = {}
                if not isinstance(context, dict):
                    context = {}
                sender = _address(context.get('sender_email') or context.get('sender') or context.get('routing_key'))
            prior = conn.execute('SELECT * FROM card_instructions WHERE source_kind=? AND source_id=?', (kind, identifier)).fetchone()
            if prior and prior['sender_email'] != sender:
                raise ValueError('The card sender changed. Reload before saving instructions.')
            unchanged = prior is not None and prior['instructions'] == text
            token = prior['revision'] if unchanged else uuid.uuid4().hex
            proposal_id = prior['proposal_decision_id'] if unchanged else None
            now = datetime.now(timezone.utc).isoformat()
            if propose_rule and proposal_id is None:
                scoped = (f'Apply this instruction only to the exact sender address {sender}. Do not broaden it to other addresses or the sender domain.\n\n' if sender else '') + text
                proposal_context = {'card_instruction_source': {'kind': kind, 'id': identifier}, 'card_instruction_exact_sender': sender, 'card_instruction_revision': token}
                cursor = conn.execute("INSERT INTO decisions(kind,summary,context,status,resolution,created_at,resolved_at) VALUES('free_text_rule',?,?,'resolved',?,?,?)", ('Rule: ' + text[:80], json.dumps(proposal_context), json.dumps({'action':'free_text_rule','text':scoped}), now, now))
                proposal_id = cursor.lastrowid
            conn.execute('INSERT INTO card_instructions VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_kind,source_id) DO UPDATE SET instructions=excluded.instructions,revision=excluded.revision,updated_at=excluded.updated_at,proposal_decision_id=excluded.proposal_decision_id', (kind,identifier,sender,text,token,prior['updated_at'] if unchanged else now,proposal_id))
            if kind == 'decision' and source['kind'] == 'vendor_mapping' and not unchanged:
                context.update(card_instruction_revision=token, suggestion_status='pending')
                context.pop('suggestion_version', None)
                context.pop('suggestion_retry_at', None)
                conn.execute('UPDATE decisions SET context=? WHERE id=?', (json.dumps(context),identifier))
        message = 'AI guidance saved for this card.'
        if propose_rule:
            message = 'AI guidance saved and a rule proposal queued. Review the proposal before mailbox policy changes.'
        return {'message':message,'note':text,'decision_id':proposal_id,'proposal_decision_id':proposal_id}
    finally:
        conn.close()
