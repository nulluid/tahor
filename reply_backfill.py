"""Evaluate recently received inbox mail once when a natural-language rule changes.

Future mail reuses its normal classification. This bounded migration never deletes,
moves, marks read, or creates a draft; the watcher handles confirmed matches afterward.
"""
from datetime import datetime, timedelta, timezone
import email
import hashlib
import re

import config
import fetch_batch
import mailbox_settings
import reply_rules
import tahor_db
from keyword_tool import store_flags


def classify_records(records):
    import backlog_worker
    free, paid = backlog_worker.classify_batch(records, mailbox_settings.get_classify_mode())
    return free + paid


def refresh_recent_matches(conn, limit=10):
    current_rules = {r['id']: r for r in reply_rules.get_rules()}
    rules = [r for r in current_rules.values() if r['match_type'] == 'natural_language']
    if not rules:
        return 0
    grace = mailbox_settings.get_inbox_grace_days()
    since = (datetime.now(timezone.utc)-timedelta(days=max(grace.values()))).strftime('%d-%b-%Y')
    candidates = set()
    for rule in rules:
        status, rows = conn.uid('SEARCH', None, 'SINCE', since, 'UNKEYWORD', reply_rules.scan_keyword(rule))
        if status != 'OK':
            raise RuntimeError('Reply-rule backfill search failed')
        candidates.update(rows[0].split() if rows and rows[0] else [])
    records, sources = [], {}
    for uid in sorted(candidates, key=int, reverse=True)[:limit]:
        status, items = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE BODY.PEEK[])')
        if status != 'OK' or not items or not isinstance(items[0], tuple):
            continue
        metadata, raw = items[0]
        match = re.search(rb'UID (\d+)', metadata)
        if not match or match[1] != uid:
            raise RuntimeError('Source UID changed during reply-rule backfill')
        message = email.message_from_bytes(raw)
        message_id = (message.get('Message-ID') or '').strip()
        original_id = message_id
        if not re.fullmatch(r'<[^<>\s]+>', message_id):
            message_id = '<reply-source-' + hashlib.sha256(raw).hexdigest() + '@localhost>'
        _, sender = email.utils.parseaddr(message.get('From',''))
        record = {'id': 'reply-scan-' + uid.decode(), 'from': sender, 'subject': fetch_batch.decode_str(message.get('Subject','')),
                  'date': message.get('Date',''), 'snippet': fetch_batch.extract_snippet(raw)}
        records.append(record)
        sources[record['id']] = (uid, sender, message_id, original_id, record['subject'])
    if not records:
        return 0
    results = classify_records(records)
    # Settings may have changed while the provider request was in flight.
    if current_rules != {r['id']: r for r in reply_rules.get_rules()}:
        return 0
    applied = 0
    for result in results:
        if result.get('action') == 'error' or result.get('id') not in sources:
            continue
        if result.get('reply_rule_versions') != {key: r.get('revision',key) for key,r in current_rules.items()}:
            continue
        uid, sender, message_id, original_id, subject = sources[result['id']]
        status, items = conn.uid('FETCH', uid, '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
        if status != 'OK' or not items or not isinstance(items[0], tuple):
            continue
        found_uid = re.search(rb'UID (\d+)', items[0][0])
        actual_id = (email.message_from_bytes(items[0][1]).get('Message-ID') or '').strip()
        if not found_uid or found_uid[1] != uid or actual_id != original_id:
            raise RuntimeError('Reply-rule source identity changed; no flags written')
        matches = result.get('reply_rule_matches', [])
        uncertain = result.get('reply_rule_uncertain', [])
        add = []
        remove = [reply_rules.keyword(r) for r in rules if r['id'] not in matches]
        if matches or uncertain:
            add += [reply_rules.PROTECTED_KEYWORD, 'retention-standard']
            add += [reply_rules.keyword(current_rules[key]) for key in matches]
            remove += ['delete-pending', 'retention-transient']
        if uncertain:
            add += ['retention-pending-review', 'needs-attention']
            tahor_db.queue_message_review('INBOX', message_id, subject, uid.decode())
        # Add protection before clearing an old deletion marker.
        if (add and not store_flags(conn, uid, add, '+')) or (remove and not store_flags(conn, uid, remove, '-')):
            raise RuntimeError('Reply-rule protection was not confirmed')
        for identifier in matches:
            tahor_db.record_reply_rule_match(identifier, message_id, sender)
        # Mark the revision complete only after every required write succeeds.
        # Otherwise a partial failure would permanently skip this message.
        if not store_flags(conn, uid, [reply_rules.scan_keyword(r) for r in rules], '+'):
            raise RuntimeError('Reply-rule scan completion was not confirmed')
        applied += 1
    return applied
