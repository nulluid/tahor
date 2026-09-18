"""Private business routing with resumable, identity-checked mailbox operations."""
import email
from email.utils import parseaddr, parsedate_to_datetime
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime, timedelta, timezone

import fetch_batch
import mailbox_settings
import tahor_db
from data_changes import atomic_write
from mailbox_paths import list_mailboxes, quote_mailbox
from mailbox_search import search_uids
from message_expiry import metadata

RECEIPT_KEYWORD = 'business-receipt'
BUSINESS_KEYWORD = 'business-correspondence'
GUARDS = {b'\\deleted', b'\\draft', b'\\flagged', b'needs-attention', b'retention-pending-review', b'reply-protected'}


def rules_path():
    return Path(os.environ.get('TAHOR_BUSINESS_RULES_PATH', Path(os.environ.get('DATA_DIR', Path(__file__).resolve().parent)) / 'business_filing.json'))


def _path(value):
    return (isinstance(value, str) and 1 <= len(value) <= 240 and value.isascii()
            and not any(ord(c) < 32 or c in '\\"*%' for c in value)
            and all(part and part not in ('.','..') for part in value.split('/'))
            and value.lower().split('/')[0] not in ('inbox','sent','drafts','trash','spam','junk'))


def load_rules():
    try:
        with rules_path().open('rb') as stream:
            raw = stream.read(131073)
    except FileNotFoundError:
        return []
    if len(raw) > 131072:
        raise ValueError('Business rules exceed the size limit')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('businesses'), list) or len(value['businesses']) > 20:
        raise ValueError('Invalid business routing configuration')
    result, identifiers = [], set()
    for business in value['businesses']:
        if (not isinstance(business, dict) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', str(business.get('id','')))
                or business['id'] in identifiers or not _path(business.get('root')) or not isinstance(business.get('rules'), list) or len(business['rules']) > 200):
            raise ValueError('Invalid business identity or folder')
        identifiers.add(business['id'])
        rule_ids = set()
        for rule in business['rules']:
            if (not isinstance(rule, dict) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', str(rule.get('id','')))
                    or not _path(rule.get('vendor')) or '/' in rule['vendor']):
                raise ValueError('Invalid business routing rule')
            if rule['id'] in rule_ids:
                raise ValueError('Duplicate business rule identity')
            rule_ids.add(rule['id'])
            if 'classified_business' in rule and type(rule['classified_business']) is not bool:
                raise ValueError('Invalid business classification scope')
            if not any(rule.get(key) for key in ('senders','domains','message_ids','classified_business')):
                raise ValueError('Business rules require an explicit source scope')
            for key in ('senders','domains','message_ids','subject_contains_any','body_contains_any'):
                items = rule.get(key, [])
                if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item,str) or not item or len(item)>1000 or any(ord(c)<32 for c in item) for item in items):
                    raise ValueError('Invalid business match values')
            for domain in rule.get('domains', []):
                if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',domain) or '..' in domain or '.' not in domain:
                    raise ValueError('Invalid business sender domain')
            for sender in rule.get('senders', []):
                if parseaddr(sender)[1] != sender or sender != sender.lower() or sender.count('@') != 1:
                    raise ValueError('Invalid business sender address')
            if rule.get('since'):
                datetime.strptime(rule['since'], '%Y-%m-%d')
            result.append(dict(rule, business_key=business['id'], root=business['root']))
    return result


def message_date(value, delivered=None):
    try:
        try:
            parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None or not 1970 <= parsed.year <= datetime.now(timezone.utc).year + 1:
            raise ValueError('Unusable message date')
        return parsed
    except (ValueError, TypeError, AttributeError, OverflowError):
        return delivered


def match_message(record, result=None, delivered=None, flags=(), rules=None):
    """Return one configured destination; ambiguous businesses are never guessed."""
    result = result or {}
    sender = parseaddr(record.get('from', record.get('sender_email','')))[1].strip().lower()
    domain = sender.rsplit('@',1)[-1] if '@' in sender else ''
    date = message_date(record.get('date',''), delivered)
    if date is None:
        return None
    subject = str(record.get('subject','')).casefold()
    body = str(record.get('body', record.get('snippet','')))[:32768].casefold()
    flags = {flag.decode().lower() if isinstance(flag,bytes) else str(flag).lower() for flag in flags}
    categories = {flag[9:] for flag in flags if flag.startswith('category-')}
    category = result.get('category') or next((item for item in ('receipt','statement','government-tax','marketing') if item in categories), '')
    matches = []
    active_rules = load_rules() if rules is None else rules
    for rule in sorted(active_rules, key=lambda item: item.get('classified_business') is True):
        if matches and rule.get('classified_business') is True:
            continue
        scoped = (sender in rule.get('senders',[]) or any(domain==item or domain.endswith('.'+item) for item in rule.get('domains',[]))
                  or record.get('id', record.get('message_id')) in rule.get('message_ids',[])
                  or (rule.get('classified_business') is True and (result.get('expense_type')=='business' or 'expense-business' in flags)))
        if not scoped or (rule.get('since') and date.date().isoformat() < rule['since']):
            continue
        if rule.get('subject_contains_any') and not any(item.casefold() in subject for item in rule['subject_contains_any']):
            continue
        if rule.get('body_contains_any') and not any(item.casefold() in body for item in rule['body_contains_any']):
            continue
        # Provider correspondence is business mail, but sales copy is never a receipt.
        receipt = category in ('receipt','statement','government-tax') or bool(re.search(r'\b(?:receipt|invoice|payment (?:received|successful|confirmation)|refund (?:issued|confirmation))\b', subject))
        if category == 'marketing' and not re.search(r'\b(?:your receipt|receipt (?:for|from)|invoice\s*(?:#|[0-9])|payment (?:received|successful|confirmation))\b', subject):
            receipt = False
        if 'refund' in subject:
            kind = 'refund'
        elif 'invoice' in subject and not re.search(r'\b(?:paid|payment received|payment successful)\b', subject + ' ' + body[:2000]):
            kind = 'invoice'
        else:
            kind = 'receipt'
        destination = rule['root'] + ('/Receipts/' + str(date.year) if receipt else '/Correspondence/' + rule['vendor'])
        matches.append(dict(business_key=rule['business_key'], matched_rule_id=rule['id'], vendor=rule['vendor'],
                            destination=destination, is_receipt=receipt, document_type=kind,
                            source_date=date.isoformat(), sender_email=sender))
    if len({(item['business_key'],item['destination']) for item in matches}) > 1:
        raise ValueError('Business rules disagree; preserve the message for review')
    return matches[0] if matches else None


def protect_classification(result, record, rules=None):
    route = match_message(record, result, rules=rules)
    if route:
        result.setdefault('business_keywords', []).append(RECEIPT_KEYWORD if route['is_receipt'] else BUSINESS_KEYWORD)
        if route['is_receipt']:
            result.update(action='keep', retention='forever', expense_type='business')
            if result.get('category') == 'marketing':
                result['category']='receipt'
    return route


def _search_quoted(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Invalid business search text')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _search_union(expressions):
    """Build a balanced binary IMAP OR without changing its source union."""
    expressions = sorted(set(expressions))
    if not expressions:
        return None
    if len(expressions) == 1:
        return expressions[0]
    middle = len(expressions) // 2
    return 'OR ' + _search_union(expressions[:middle]) + ' ' + _search_union(expressions[middle:])


def _candidate_search(conn, rules, after):
    conditions = []
    for rule in rules:
        sources = []
        for value in rule.get('senders', []) + rule.get('domains', []):
            sources.append('FROM ' + _search_quoted(value) if value.isascii() else 'ALL')
        for value in rule.get('message_ids', []):
            sources.append('HEADER Message-ID ' + _search_quoted(value) if value.isascii() else 'ALL')
        if rule.get('classified_business') is True:
            sources.append('KEYWORD expense-business')
        source = 'ALL' if 'ALL' in sources else _search_union(sources)
        if source is None:
            continue
        subjects = rule.get('subject_contains_any', [])
        # Without a negotiated Unicode search charset, preserve a broad source
        # search if ANY alternative is non-ASCII. The local matcher remains
        # authoritative for all subject/body filters and header-date cutoffs.
        if subjects and all(value.isascii() for value in subjects):
            subject = _search_union(['SUBJECT ' + _search_quoted(value) for value in subjects])
            conditions.append('(' + source + ' ' + subject + ')')
        else:
            conditions.append(source)
    expression = _search_union(conditions)
    if expression is None:
        return []
    criteria = ['UID', str(after + 1) + ':*', expression]
    status, values = search_uids(conn,*criteria)
    if status != 'OK':
        raise RuntimeError('Business inventory search failed')
    uids = values[0].split() if values and values[0] else []
    if any(not uid.isdigit() for uid in uids):
        raise RuntimeError('Invalid business inventory identity')
    return sorted((uid for uid in uids if int(uid)>after),key=int)


def _fetch(conn, mailbox, validity, uid):
    status, rows = conn.uid('FETCH',uid,'(UID FLAGS INTERNALDATE BODY.PEEK[]<0.32768>)')
    if status!='OK':
        raise RuntimeError('Business message read failed')
    if not any(isinstance(row,tuple) for row in rows or []):
        return None
    _, flags, delivered = metadata(rows,uid,content=True)
    raw = next(row[1] for row in rows if isinstance(row,tuple))
    if len(raw)>32768:
        raise RuntimeError('Business message exceeds bounded prefix')
    message=email.message_from_bytes(raw)
    ids=message.get_all('Message-ID',[])
    if len(ids)>1:
        raise RuntimeError('Ambiguous business message identity')
    identifier=str(ids[0]).strip() if ids else fetch_batch.local_message_id(mailbox,validity,uid.decode())
    record=dict(id=identifier,from_header=str(message.get('From','')),subject=fetch_batch.decode_str(message.get('Subject','')),
                date=str(message.get('Date','')),body=fetch_batch.extract_body_text(raw))
    record['from']=record.pop('from_header')
    if not ids:
        record['ledger_id'] = '<tahor-content-' + hashlib.sha256(raw).hexdigest() + '@localhost>'
    return record,flags,delivered


def _run_sweep(conn, *, backfill=False, limit=100, budget_seconds=45, dry_run=False, state_path=None):
    """Resume across every selectable folder; no folder-size ceiling or DELETE."""
    rules=load_rules()
    if not rules:
        return dict(examined=0,moved=0,receipts=0,deferred=0,complete=True)
    path=Path(state_path or tahor_db.DB_PATH.parent/'business_filing_state.json')
    fingerprint=hashlib.sha256(json.dumps(rules,sort_keys=True).encode()).hexdigest()
    try:state=json.loads(path.read_text())
    except FileNotFoundError:state={}
    if state.get('rules')!=fingerprint:
        state=dict(rules=fingerprint,folders={},next_folder='')
    paths=sorted(name for name,flags in list_mailboxes(conn) if '\\drafts' not in flags and name.lower()!='drafts')
    visited = set(state.get('visited', []))
    paths = [name for name in paths if name not in visited]
    start=state.get('next_folder','')
    paths=[name for name in paths if name>=start]+[name for name in paths if name<start]
    counts=dict(examined=0,moved=0,receipts=0,deferred=0,complete=False)
    deadline=time.monotonic()+budget_seconds
    created=set()
    from filing_sweep import ensure_folder
    for index,source in enumerate(paths):
        if counts['examined']>=limit or time.monotonic()>=deadline:break
        state['next_folder']=source
        if conn.select(quote_mailbox(source),readonly=dry_run)[0]!='OK':
            raise RuntimeError('Business source mailbox unavailable')
        validity=fetch_batch.mailbox_uidvalidity(conn)
        if not validity:
            raise RuntimeError('Business source UIDVALIDITY unavailable')
        cursor=state['folders'].get(source,{})
        after=cursor.get('after',0) if cursor.get('uidvalidity')==validity else 0
        deferred=set(cursor.get('deferred', [])) if cursor.get('uidvalidity')==validity else set()
        uids=sorted(set(_candidate_search(conn,rules,after)) | {str(uid).encode() for uid in deferred if str(uid).isdigit()},key=int)
        finished=True
        for uid in uids:
            if counts['examined']>=limit or time.monotonic()>=deadline:
                finished=False;break
            counts['examined']+=1
            deferred.discard(int(uid))
            fetched=_fetch(conn,source,validity,uid)
            if fetched:
                record,flags,delivered=fetched
                route=match_message(record,delivered=delivered,flags=flags,rules=rules)
                if route and b'\\draft' not in flags:
                    if route['is_receipt']:
                        counts['receipts']+=1
                    blocked=bool(flags & GUARDS)
                    age_ok=source.upper()!='INBOX' or backfill or datetime.now(timezone.utc)>=delivered+timedelta(days=mailbox_settings.get_inbox_grace_days()['read' if b'\\seen' in flags else 'unread'])
                    move=not blocked and age_ok and source!=route['destination']
                    if not dry_run:
                        # Recheck the same selected mailbox generation and exact source.
                        if conn.select(quote_mailbox(source), readonly=False)[0] != 'OK' or fetch_batch.mailbox_uidvalidity(conn) != validity:
                            raise RuntimeError('Business mailbox generation changed')
                        status,current=conn.uid('FETCH',uid,'(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
                        if status!='OK':raise RuntimeError('Business source revalidation failed')
                        _,current_flags,current_date=metadata(current,uid,content=True)
                        current_raw=next(row[1] for row in current if isinstance(row,tuple))
                        current_ids=email.message_from_bytes(current_raw).get_all('Message-ID',[])
                        exact=(len(current_ids)==1 and str(current_ids[0]).strip()==record['id']) or (not current_ids and record['id']==fetch_batch.local_message_id(source,validity,uid.decode()))
                        if not exact or current_flags!=flags or current_date!=delivered:
                            raise RuntimeError('Business source changed; retry without moving it')
                        keywords=[RECEIPT_KEYWORD,'retention-forever','expense-business'] if route['is_receipt'] else [BUSINESS_KEYWORD]
                        if conn.uid('STORE',uid,'+FLAGS.SILENT','('+' '.join(keywords)+')')[0]!='OK':raise RuntimeError('Business receipt protection failed')
                        if route['is_receipt']:
                            obsolete = [flag.decode('ascii') for flag in flags
                                        if flag.startswith(b'retention-') and flag not in
                                        {b'retention-forever', b'retention-pending-review'}
                                        or flag in {b'delete-pending', b'expense-personal'}]
                            if obsolete and conn.uid('STORE', uid, '-FLAGS.SILENT', '('+' '.join(obsolete)+')')[0] != 'OK':
                                raise RuntimeError('Business obsolete deletion markers could not be cleared')
                            import business_ledger
                            business_ledger.record_receipt(dict(route,mailbox=source,message_id=record.get('ledger_id',record['id']),uid=uid.decode(),uidvalidity=validity,received_at=delivered.isoformat(),subject=record['subject']),record['body'],verified_business=True)
                        if move:
                            capabilities={v.decode().upper() if isinstance(v,bytes) else v.upper() for v in conn.capabilities}
                            if 'MOVE' not in capabilities:raise RuntimeError('Business filing requires IMAP MOVE')
                            ensure_folder(conn,route['destination'],created)
                            if conn.select(quote_mailbox(source), readonly=False)[0] != 'OK' or fetch_batch.mailbox_uidvalidity(conn) != validity:
                                raise RuntimeError('Business mailbox generation changed before move')
                            checked = _fetch(conn, source, validity, uid)
                            if not checked or checked[0]['id'] != record['id'] or checked[2] != delivered or checked[1] & GUARDS:
                                raise RuntimeError('Business source changed before move')
                            move_status, move_data = conn.uid('MOVE',uid,quote_mailbox(route['destination']))
                            if move_status != 'OK':raise RuntimeError('Business filing move failed')
                            response = conn.response('COPYUID')
                            copy_values = response[1] if response and response[0] == 'COPYUID' else []
                            for value in copy_values or []:
                                match = re.fullmatch(rb'([1-9][0-9]*) ([1-9][0-9]*) ([1-9][0-9]*)', value or b'')
                                if match and int(match[2]) == int(uid) and all(int(v) <= 4294967295 for v in match.groups()):
                                    if route['is_receipt']:
                                        business_ledger.record_receipt(dict(route, mailbox=route['destination'],message_id=record.get('ledger_id',record['id']),uid=match[3].decode(),uidvalidity=match[1].decode(),received_at=delivered.isoformat(),subject=record['subject']),record['body'],verified_business=True)
                                    break
                            tahor_db.relocate_vendor_samples(source,route['destination'],[record['id']])
                    if move:counts['moved']+=1
                    elif source!=route['destination']:
                        counts['deferred']+=1
                        deferred.add(int(uid))
            after=max(after,int(uid))
            state['folders'][source]=dict(uidvalidity=validity,after=after,deferred=sorted(deferred))
            if not dry_run:atomic_write(path,json.dumps(state)+'\n')
        if finished:
            visited.add(source)
            state['visited'] = sorted(visited)
            state['next_folder']=paths[index+1] if index+1<len(paths) else ''
            if index+1==len(paths):
                counts['complete']=True
                state['visited'] = []
        if not dry_run:atomic_write(path,json.dumps(state)+'\n')
    return counts


def run_sweep(conn, **options):
    """Serialize resumable business scans across workers and manual backfills."""
    if not load_rules():
        return dict(examined=0,moved=0,receipts=0,deferred=0,complete=True)
    state = Path(options.get('state_path') or Path(tahor_db.DB_PATH).parent / 'business_filing_state.json')
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(examined=0,moved=0,receipts=0,deferred=0,complete=False,busy=True)
        return _run_sweep(conn, **options)
