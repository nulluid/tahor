"""Private receipt evidence and reviewable amounts; not tax or accounting advice."""
import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import unicodedata

# Unsupported currencies remain reviewable rather than assuming decimal places.
UNITS = {'USD': 2, 'EUR': 2, 'GBP': 2, 'CAD': 2, 'AUD': 2, 'NZD': 2,
         'CHF': 2, 'SEK': 2, 'NOK': 2, 'DKK': 2, 'JPY': 0, 'KRW': 0,
         'KWD': 3, 'BHD': 3}
KINDS = ('receipt', 'invoice', 'refund')
SCHEMA = '''CREATE TABLE IF NOT EXISTS business_ledger (
 id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
 business_key TEXT NOT NULL, matched_rule_id TEXT NOT NULL,
 vendor TEXT NOT NULL, sender_email TEXT NOT NULL,
 mailbox TEXT NOT NULL, message_id TEXT NOT NULL, uid TEXT NOT NULL, uidvalidity TEXT NOT NULL,
 received_at TEXT NOT NULL, document_date TEXT, date_basis TEXT NOT NULL,
 document_type TEXT NOT NULL, reference TEXT, currency TEXT, amount_minor INTEGER,
 status TEXT NOT NULL, review_reasons TEXT NOT NULL, duplicate_of INTEGER,
 source_digest TEXT NOT NULL, owner_confirmed INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL)'''


def _db():
    import tahor_db
    db = tahor_db.get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(SCHEMA)
        columns={row['name'] for row in db.execute('PRAGMA table_info(business_ledger)')}
        for name in ('comment','category','category_suggestion','category_suggestion_reason','subject','amount_candidate','ai_suggestion_reason'):
            if name not in columns:
                db.execute('ALTER TABLE business_ledger ADD COLUMN '+name+" TEXT NOT NULL DEFAULT ''")
        if 'ai_suggestions' not in columns:
            db.execute("ALTER TABLE business_ledger ADD COLUMN ai_suggestions TEXT NOT NULL DEFAULT '{}'")
        if 'metadata_confirmed' not in columns:
            db.execute('ALTER TABLE business_ledger ADD COLUMN metadata_confirmed INTEGER NOT NULL DEFAULT 0')
        db.execute('CREATE TABLE IF NOT EXISTS business_ledger_reviews (id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,changed_at TEXT NOT NULL)')
    return db


def _text(value, limit=500):
    return str(value or '').strip()[:limit]


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).date().isoformat()
        except ValueError:
            return None


def _minor(number, currency):
    if currency not in UNITS:
        return None
    # A comma is only a correctly grouped thousands separator. Locale-ambiguous
    # decimal commas are left for the owner; no rounding is performed.
    if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', number):
        return None
    try:
        amount = Decimal(number.replace(',', ''))
        places = max(0, -amount.as_tuple().exponent)
        scaled = amount * (10 ** UNITS[currency])
        if places > UNITS[currency] or scaled > 10**14:
            return None
        return int(scaled)
    except (InvalidOperation, ValueError):
        return None


def _money(value, context_currency=None, allow_negative=False):
    codes = re.findall(r'\b[A-Z]{3}\b', value)
    if len(set(codes)) > 1:
        return None
    currency = codes[0] if codes else context_currency
    if currency not in UNITS:
        return None
    cleaned = re.sub(r'\b[A-Z]{3}\b', '', value).strip()
    cleaned = re.sub(r'^[\$€£¥]\s*', '', cleaned).strip()
    if '$' in value and currency not in ('USD','CAD','AUD','NZD'):
        return None
    if allow_negative and cleaned.startswith('-'):
        cleaned = cleaned[1:]
    if any(symbol in value for symbol in '€£¥'):
        expected = {'€':'EUR', '£':'GBP', '¥':'JPY'}
        if any(symbol in value and currency != code for symbol, code in expected.items()):
            return None
    amount = _minor(cleaned, currency)
    return (currency, amount) if amount is not None else None


_TOTAL_LABELS = r'grand total|order total|total paid|amount paid|total charged|amount charged|refund total|amount refunded|amount credited|invoice total|total due|amount due|total'
_NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
_MONEY_TOKEN = r'(?:[A-Z]{3}\s*)?[$€£¥]?\s*-?' + _NUMBER + r'(?:\s*[A-Z]{3}\b)?'


def is_payment_reminder(subject, text):
    source=(str(subject or '')+' '+str(text or ''))[:201000].lower()
    reminder=bool(re.search(r'\b(?:payment (?:reminder|scheduled|upcoming|is due|due soon)|upcoming payment|scheduled payment|payment is coming|payment will be|will (?:be charged|charge|debit)|(?:reminder|reminding).{0,60}(?:payment|pay)|pay.{0,30}due (?:soon|on))\b',source))
    completed=bool(re.search(r'\b(?:payment (?:received|successful|completed)|paid in full|amount paid|total paid|total charged|amount charged|your receipt|payment receipt|purchase receipt|receipt for|receipt from|refund (?:issued|processed|completed))\b',source))
    substantive_invoice=bool(re.search(r'\binvoice\s*(?:#|id\b|number\b|no\.?)\s*:?\s*[a-z0-9][a-z0-9._/-]+',source) and re.search(r'\b(?:amount due|invoice total)\s*[:\-]?\s*(?:[a-z]{3}\s*)?[$€£¥]?\s*\d',source))
    return reminder and not completed and not substantive_invoice


def _amount_values(text):
    # Mail extraction and HTML tables can collapse labels and totals onto one
    # line. Match exact labelled monetary tokens instead of relying on newlines.
    pattern=re.compile(r'(?<![\w])('+_TOTAL_LABELS+r')\s*[:\-]?\s*('+_MONEY_TOKEN+r')(?![\d.,])',re.I)
    values=[]
    for match in pattern.finditer(text):
        value=match[2].strip()
        tail_start=match.end()
        # Currency codes are uppercase evidence, not the next ordinary word.
        trailing=re.search(r'\s+([A-Za-z]{3})$',value)
        if trailing and trailing[1]!=trailing[1].upper():
            tail_start=match.start(2)+trailing.start()
            value=value[:trailing.start()].strip()
        tail=text[tail_start:]
        if re.match(r'\s*(?:and|or|[+/])\s*(?:[A-Z]{3}\s*)?[$€£¥]?\s*\d',tail):
            value+=' ambiguous'
        values.append((match[1],value))
    return values


def _candidate_amount(value):
    cleaned=re.sub(r'\b[A-Z]{3}\b','',value).strip()
    cleaned=re.sub(r'^[$€£¥]\s*','',cleaned).strip().lstrip('-')
    if not re.fullmatch(_NUMBER,cleaned): return None
    try:
        amount=Decimal(cleaned.replace(',',''))
        if amount>10**14 or -amount.as_tuple().exponent>6: return None
        return format(amount,'f')
    except InvalidOperation: return None


def _receipt_text(value):
    text=str(value or '')[:200000]
    if not re.search(r'<(?:html|body|table|div|p|td|span)(?:\s|>)',text,re.I):
        return text
    class PlainReceipt(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts=[];self.suppressed=0
        def handle_starttag(self,tag,attrs):
            if tag in ('script','style','head'): self.suppressed+=1
            if not self.suppressed: self.parts.append('\n' if tag in ('div','p','tr','td','br','table') else ' ')
        def handle_endtag(self,tag):
            if tag in ('script','style','head') and self.suppressed: self.suppressed-=1
            if not self.suppressed: self.parts.append('\n' if tag in ('div','p','tr','td','table') else ' ')
        def handle_data(self,data):
            if not self.suppressed: self.parts.append(data)
    parser=PlainReceipt();parser.feed(text);parser.close()
    return ''.join(parser.parts)


def extract(text, subject='', source_date=None):
    """Extract explicit totals only. Ambiguous evidence is never silently guessed."""
    text = _receipt_text(text)
    source = str(subject or '')[:1000] + '\n' + text
    lower = source.lower()
    pending = bool(re.search(r'\b(?:payment|refund)\s+(?:pending|declined|failed|requested)|\bnot paid\b|\bunpaid\b', lower))
    refunded = bool(re.search(r'\b(?:refund (?:issued|processed|completed|receipt|total)|amount refunded|you (?:have been|were) refunded|(?:credited|refunded) to (?:your |the )?(?:original payment method|credit card|debit card|bank account))\b', lower))
    store_credit = bool(re.search(r'\b(?:store credit|merchandise credit|credit voucher|gift card credit)\b',lower))
    paid = bool(re.search(r'\b(?:payment (?:received|successful|completed)|paid in full|amount paid|total paid|total charged|amount charged)\b', lower))
    receipt = bool(re.search(r'\b(?:your receipt|payment receipt|purchase receipt|receipt for|receipt from)\b', lower))
    kind = 'refund' if refunded and not pending else 'receipt' if (paid or receipt) and not pending else 'invoice' if re.search(r'\binvoice\b|\bamount due\b', lower) else 'unknown'
    reasons = []
    if store_credit and refunded:
        kind='unknown'
        reasons.append('noncash_credit_requires_review')
    if is_payment_reminder(subject,text):
        kind='unknown'
        reasons.append('payment_reminder_not_receipt')
    if kind == 'unknown':
        reasons.append('document_type_unconfirmed')
    labels = _amount_values(text)
    context_codes = re.findall(r'\bCurrency\s*:\s*([A-Z]{3})\b', text)
    context_currency = context_codes[0] if len(set(context_codes)) == 1 else None
    preferred = {'receipt': {'amount paid','total paid','total charged','amount charged'}, 'refund': {'refund total','amount refunded','amount credited'}, 'invoice': {'invoice total','amount due','total due'}}.get(kind, set())
    primary = [(label, value) for label, value in labels if label.lower() in preferred]
    candidates = primary or [(label, value) for label, value in labels if label.lower() in ('grand total','order total','total')]
    parsed = [_money(value.strip(), context_currency, allow_negative=kind == 'refund') for _, value in candidates]
    distinct = {amount for amount in parsed if amount is not None}
    proposed=[_candidate_amount(value) for _,value in candidates]
    amount_candidate=proposed[0] if proposed and None not in proposed and len(set(proposed))==1 else ''
    if not candidates or any(amount is None for amount in parsed) or len(distinct) != 1:
        currency = amount_minor = None
        reasons.append('currency_unconfirmed' if amount_candidate and not any(re.search(r'\b[A-Z]{3}\b',value) for _,value in candidates) and not context_codes else ('amount_or_currency_ambiguous' if candidates else 'amount_missing'))
    else:
        currency, amount_minor = distinct.pop()
        if kind == 'refund':
            amount_minor = -amount_minor
    explicit_dates = re.findall(r'^\s*(?:invoice date|receipt date|payment date|refund date|date)\s*:\s*(\d{4}-\d{2}-\d{2})\s*$', text, re.I | re.M)
    valid_dates = {_date(value) for value in explicit_dates}
    if len(valid_dates) == 1 and None not in valid_dates:
        document_date, date_basis = valid_dates.pop(), 'document'
    elif not explicit_dates and _date(source_date):
        document_date, date_basis = _date(source_date), 'email_date'
    else:
        document_date, date_basis = None, 'unknown'
        reasons.append('date_missing_or_ambiguous')
    references = re.findall(r'^\s*(?:invoice|receipt|order|refund|transaction)\s*(?:number|no\.?|id|#)\s*:?\s*([A-Za-z0-9][A-Za-z0-9._/-]{1,79})\s*$', text, re.I | re.M)
    reference = references[0] if len(set(references)) == 1 else None
    if len(set(references)) > 1:
        reasons.append('multiple_references')
    return dict(document_type=kind, currency=currency, amount_minor=amount_minor,amount_candidate=amount_candidate,
                document_date=document_date, date_basis=date_basis, reference=reference,
                status='review_needed' if reasons else 'ready', review_reasons=reasons)


def record_receipt(metadata, text, verified_business=False):
    """Only the verified business-filing path may add automatic source records."""
    if verified_business is not True or not isinstance(metadata, dict):
        raise ValueError('Verified business receipt evidence is required')
    required = ('business_key','matched_rule_id','mailbox','message_id','sender_email','uid','uidvalidity')
    if any(not isinstance(metadata.get(key), (str, int)) or not str(metadata[key]).strip() for key in required):
        raise ValueError('Business rule and exact message identity are required')
    for key in ('uid','uidvalidity'):
        if not re.fullmatch(r'[1-9][0-9]{0,9}', str(metadata[key])) or int(metadata[key]) > 4294967295:
            raise ValueError('A verified numeric UID and UIDVALIDITY are required')
    if len(str(metadata['message_id'])) > 2048 or len(str(metadata['business_key'])) > 200:
        raise ValueError('Message identity or business key is too long')
    sender = _text(metadata['sender_email'], 320).lower()
    if not re.fullmatch(r'[^\s<>"\\@]+@[^\s<>"\\@]+', sender):
        raise ValueError('An exact sender address is required')
    business = _text(metadata['business_key'], 200)
    identifier = _text(metadata['message_id'], 2048)
    source_key = hashlib.sha256(json.dumps([business,sender,identifier]).encode()).hexdigest()
    result = extract(text, metadata.get('subject',''), metadata.get('source_date') or metadata.get('received_at'))
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(str(text or '').encode()).hexdigest()
    db = _db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM business_ledger WHERE source_key=?',(source_key,)).fetchone()
            if previous:
                if previous['source_digest'] != digest:
                    reasons = json.loads(previous['review_reasons'])
                    if 'source_identity_collision' not in reasons:
                        reasons.append('source_identity_collision')
                        db.execute("UPDATE business_ledger SET status='review_needed',review_reasons=?,updated_at=? WHERE id=?", (json.dumps(reasons),now,previous['id']))
                        _audit(db,previous)
                    return previous['id']
                if 'source_identity_collision' in json.loads(previous['review_reasons']):
                    return previous['id']
                # Repeated scans and moved copies refresh location, not reviewed amounts.
                db.execute('UPDATE business_ledger SET mailbox=?,uid=?,uidvalidity=?,updated_at=? WHERE id=?', (_text(metadata['mailbox'],1000),str(metadata['uid']),str(metadata['uidvalidity']),now,previous['id']))
                return previous['id']
            duplicate = None
            if result['reference']:
                duplicate = db.execute('SELECT id FROM business_ledger WHERE business_key=? AND sender_email=? AND document_type=? AND reference=? ORDER BY id LIMIT 1', (business,sender,result['document_type'],result['reference'])).fetchone()
            if not duplicate:
                duplicate = db.execute('SELECT id FROM business_ledger WHERE business_key=? AND sender_email=? AND document_type=? AND source_digest=? AND document_date IS ? ORDER BY id LIMIT 1', (business,sender,result['document_type'],digest,result['document_date'])).fetchone()
            if duplicate:
                result['status'] = 'review_needed'
                result['review_reasons'].append('possible_duplicate_document')
            values = (source_key,business,_text(metadata['matched_rule_id'],200),_text(metadata.get('vendor') or sender),sender,_text(metadata['mailbox'],1000),identifier,str(metadata['uid']),str(metadata['uidvalidity']),_text(metadata.get('received_at'),100),result['document_date'],result['date_basis'],result['document_type'],result['reference'],result['currency'],result['amount_minor'],result['status'],json.dumps(result['review_reasons']),duplicate['id'] if duplicate else None,digest,now,now)
            row = db.execute('INSERT INTO business_ledger(source_key,business_key,matched_rule_id,vendor,sender_email,mailbox,message_id,uid,uidvalidity,received_at,document_date,date_basis,document_type,reference,currency,amount_minor,status,review_reasons,duplicate_of,source_digest,created_at,updated_at) VALUES('+','.join('?' for _ in values)+')',values)
            db.execute('UPDATE business_ledger SET subject=?,amount_candidate=? WHERE id=?',(_text(metadata.get('subject'),1000),result['amount_candidate'],row.lastrowid))
            return row.lastrowid
    finally:
        db.close()


def list_entries(business_key=None, limit=None, year=None):
    db = _db()
    try:
        clauses=[]; parameters=[]
        if business_key:
            clauses.append('business_key=?');parameters.append(business_key)
        if year is not None:
            year=_year(year)
            clauses.append("substr(COALESCE(NULLIF(document_date,''),received_at),1,4)=?");parameters.append(str(year))
        query='SELECT * FROM business_ledger'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY document_date DESC,id DESC'
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 10000:
                raise ValueError('Choose a ledger page limit between 1 and 10000')
            query += ' LIMIT ?'
            parameters.append(limit)
        rows = db.execute(query, parameters)
        result = []
        for row in rows:
            item = dict(row)
            item['review_reasons'] = json.loads(item['review_reasons'])
            result.append(item)
        return result
    finally:
        db.close()


def _year(value):
    if type(value) is not int or not 1 <= value <= 9999:
        raise ValueError('Choose a valid calendar year')
    return value


def _metadata_text(value, limit, multiline=False):
    if not isinstance(value,str) or len(value)>limit or any(unicodedata.category(c) in ('Cc','Cs') and (not multiline or c not in '\n\r\t') for c in value):
        raise ValueError('Invalid expense metadata')
    return value.strip()


def update_metadata(identifier, *, comment, category):
    comment=_metadata_text(comment,4000,True);category=_metadata_text(category,120)
    db=_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if row is None: raise LookupError('Ledger entry not found')
            db.execute('UPDATE business_ledger SET comment=?,category=?,metadata_confirmed=1,updated_at=? WHERE id=?',(comment,category,datetime.now(timezone.utc).isoformat(),identifier))
            _audit(db,row)
    finally: db.close()


def set_category_suggestion(identifier, category, reason, expected_source_digest=None):
    category=_metadata_text(category,120);reason=_metadata_text(reason,500)
    db=_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if row is None: raise LookupError('Ledger entry not found')
            if row['category'] or (expected_source_digest is not None and row['source_digest']!=expected_source_digest):
                return False
            db.execute('UPDATE business_ledger SET category_suggestion=?,category_suggestion_reason=?,updated_at=? WHERE id=?',(category,reason,datetime.now(timezone.utc).isoformat(),identifier))
            _audit(db,row)
            return True
    finally: db.close()


def set_ai_suggestions(identifier, fields, reason, expected_source_digest=None):
    """Store reviewable model proposals separately from every authoritative field."""
    limits={'vendor':500,'document_date':10,'document_type':20,'reference':80,'amount':80,'currency':3,'category':120,'comment':4000}
    if not isinstance(fields,dict) or not fields or set(fields)-set(limits):
        raise ValueError('Invalid expense proposal fields')
    proposal={}
    for key,value in fields.items():
        proposal[key]=None if value is None else _metadata_text(value,limits[key],key=='comment')
    if proposal.get('document_date') is not None and _date(proposal['document_date'])!=proposal['document_date']:
        raise ValueError('Invalid proposed document date')
    if proposal.get('document_type') is not None and proposal['document_type'] not in KINDS:
        raise ValueError('Invalid proposed document type')
    if proposal.get('currency') is not None and proposal['currency'] not in UNITS:
        raise ValueError('Invalid proposed currency')
    amount=proposal.get('amount')
    if amount is not None:
        if not re.fullmatch(r'\d+(?:\.\d{1,6})?',amount) or Decimal(amount)>10**14:
            raise ValueError('Invalid proposed amount')
        if proposal.get('currency') and _minor(amount,proposal['currency']) is None:
            raise ValueError('Proposed amount exceeds currency precision')
    reason=_metadata_text(reason,500)
    db=_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if row is None: raise LookupError('Ledger entry not found')
            if expected_source_digest is not None and row['source_digest']!=expected_source_digest:
                return False
            db.execute('UPDATE business_ledger SET ai_suggestions=?,ai_suggestion_reason=?,updated_at=? WHERE id=?',(json.dumps(proposal,sort_keys=True),reason,datetime.now(timezone.utc).isoformat(),identifier))
            _audit(db,row)
            return True
    finally: db.close()


def accept_category_suggestion(identifier, expected_suggestion):
    """Accept the reviewed suggestion without overwriting concurrent comments."""
    expected_suggestion=_metadata_text(expected_suggestion,120)
    if not expected_suggestion: raise ValueError('Choose a category suggestion to accept')
    db=_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if row is None: raise LookupError('Ledger entry not found')
            if row['category_suggestion']!=expected_suggestion or row['category']:
                raise ValueError('The category changed; refresh before accepting this suggestion')
            db.execute('UPDATE business_ledger SET category=?,metadata_confirmed=1,updated_at=? WHERE id=?',(expected_suggestion,datetime.now(timezone.utc).isoformat(),identifier))
            _audit(db,row)
    finally: db.close()


def reprocess_entry(identifier, text, subject='', source_date=None):
    """Re-extract verified archived source; preserve the original source digest.

    Caller must verify original EML identity before supplying richer evidence.
    Source collisions, exclusions and owner-confirmed amounts cannot be replaced.
    """
    db=_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if row is None: raise LookupError('Ledger entry not found')
            reasons=json.loads(row['review_reasons'])
            if row['owner_confirmed'] or row['status']=='excluded' or 'source_identity_collision' in reasons:
                return False
            result=extract(text,subject or row['subject'],source_date or row['document_date'] or row['received_at'])
            duplicate_of=row['duplicate_of']
            if not duplicate_of and result['reference']:
                other=db.execute('SELECT id FROM business_ledger WHERE id!=? AND business_key=? AND sender_email=? AND document_type=? AND reference=? ORDER BY id LIMIT 1',(identifier,row['business_key'],row['sender_email'],result['document_type'],result['reference'])).fetchone()
                duplicate_of=other['id'] if other else None
            if duplicate_of:
                result['review_reasons'].append('possible_duplicate_document')
                result['status']='review_needed'
            db.execute('UPDATE business_ledger SET document_type=?,currency=?,amount_minor=?,document_date=?,date_basis=?,reference=?,status=?,review_reasons=?,subject=?,amount_candidate=?,duplicate_of=?,updated_at=? WHERE id=?',
                       (result['document_type'],result['currency'],result['amount_minor'],result['document_date'],result['date_basis'],result['reference'],result['status'],json.dumps(result['review_reasons']),_text(subject or row['subject'],1000),result['amount_candidate'],duplicate_of,datetime.now(timezone.utc).isoformat(),identifier))
            _audit(db,row)
            return True
    finally: db.close()


def confirm_entry(identifier, *, document_type, currency, amount, document_date, reference=None, distinct_document=False, vendor=None, category=None, comment=None):
    """Explicit owner review supplies uncertain values; invoices stay separate."""
    if type(distinct_document) is not bool:
        raise ValueError('Duplicate confirmation must be explicit')
    if document_type not in KINDS or currency not in UNITS or not _date(document_date):
        raise ValueError('Choose a supported document type, currency, and ISO date')
    minor = _minor(str(amount), currency)
    if minor is None:
        raise ValueError('Enter an unsigned exact amount in the selected currency')
    if document_type == 'refund':
        minor = -minor
    metadata={}
    for key,value,limit in (('vendor',vendor,500),('category',category,120),('comment',comment,4000)):
        if value is not None: metadata[key]=_metadata_text(value,limit,key=='comment')
    if vendor is not None and not metadata['vendor']: raise ValueError('Enter a vendor name')
    db = _db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if row is None:
                raise LookupError('Ledger entry not found')
            if 'source_identity_collision' in json.loads(row['review_reasons']):
                raise ValueError('Conflicting source identity requires source reconciliation; exclude this entry until resolved')
            if row['duplicate_of'] and not distinct_document:
                raise ValueError('Resolve the possible duplicate outside automatic totals before confirming this entry')
            db.execute("UPDATE business_ledger SET document_type=?,currency=?,amount_minor=?,document_date=?,date_basis='owner',reference=?,status='ready',review_reasons='[]',owner_confirmed=1,duplicate_of=NULL,updated_at=? WHERE id=?", (document_type,currency,minor,_date(document_date),(_text(reference,80) if reference is not None else row['reference']),datetime.now(timezone.utc).isoformat(),identifier))
            if metadata:
                assignments=','.join(key+'=?' for key in metadata)
                db.execute('UPDATE business_ledger SET '+assignments+',metadata_confirmed=1 WHERE id=?',(*metadata.values(),identifier))
            _audit(db, row)
    finally:
        db.close()


def _audit(db, before):
    after = db.execute('SELECT * FROM business_ledger WHERE id=?', (before['id'],)).fetchone()
    db.execute('INSERT INTO business_ledger_reviews(entry_id,before_json,after_json,changed_at) VALUES(?,?,?,?)', (before['id'],json.dumps(dict(before)),json.dumps(dict(after)),datetime.now(timezone.utc).isoformat()))


def get_entry(identifier):
    db = _db()
    try:
        row = db.execute('SELECT * FROM business_ledger WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise LookupError('Ledger entry not found')
        result = dict(row)
        result['review_reasons'] = json.loads(result['review_reasons'])
        return result
    finally:
        db.close()


def exclude_entry(identifier):
    """Owner-confirmed exclusion retains evidence and an audit record."""
    db = _db()
    try:
        with db:
            row = db.execute('SELECT * FROM business_ledger WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise LookupError('Ledger entry not found')
            db.execute("UPDATE business_ledger SET status='excluded',owner_confirmed=1,updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(),identifier))
            _audit(db,row)
    finally:
        db.close()


def exclude_non_receipt(identifier):
    """Verified source inspection may hide reminders without owner confirmation."""
    db=_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if row is None: raise LookupError('Ledger entry not found')
            reasons=json.loads(row['review_reasons'])
            if row['owner_confirmed'] or row['status']=='excluded' or 'source_identity_collision' in reasons:
                return False
            if 'not_receipt' not in reasons: reasons.append('not_receipt')
            db.execute("UPDATE business_ledger SET status='excluded',review_reasons=?,updated_at=? WHERE id=?",(json.dumps(reasons),datetime.now(timezone.utc).isoformat(),identifier))
            _audit(db,row)
            return True
    finally: db.close()


def _amount(minor, currency):
    return format(Decimal(minor) / (10 ** UNITS[currency]), '.'+str(UNITS[currency])+'f')


def summaries(business_key=None, year=None, rows=None):
    groups = {}
    for row in (list_entries(business_key,year=year) if rows is None else rows):
        if row['status'] != 'ready' or row['duplicate_of'] or row['document_type'] not in KINDS or row['currency'] not in UNITS or type(row['amount_minor']) is not int:
            continue
        key = (row['business_key'],row['currency'])
        group = groups.setdefault(key,{'business_key':key[0],'currency':key[1],'receipts_minor':0,'refunds_minor':0,'invoices_minor':0})
        group[{'receipt':'receipts_minor','refund':'refunds_minor','invoice':'invoices_minor'}[row['document_type']]] += row['amount_minor']
    return [dict(group, net_paid_minor=group['receipts_minor']+group['refunds_minor']) for group in groups.values()]


def report(year, business_key=None, rows=None):
    year=_year(year)
    grouped={key:{} for key in ('months','categories','monthly_categories')}
    for row in (list_entries(business_key,year=year) if rows is None else rows):
        if row['status']!='ready' or row['duplicate_of'] or row['document_type'] not in KINDS or row['currency'] not in UNITS or type(row['amount_minor']) is not int:
            continue
        month=(row['document_date'] or row['received_at'])[:7]
        category=row['category'] or 'Uncategorized'
        for section,extra in (('months',{'month':month}),('categories',{'category':category}),('monthly_categories',{'month':month,'category':category})):
            key=(row['business_key'],row['currency'],*extra.values())
            group=grouped[section].setdefault(key,dict(business_key=row['business_key'],currency=row['currency'],receipts_minor=0,refunds_minor=0,invoices_minor=0,**extra))
            group[{'receipt':'receipts_minor','refund':'refunds_minor','invoice':'invoices_minor'}[row['document_type']]]+=row['amount_minor']
    return dict(year=year,totals=summaries(business_key,year=year,rows=rows),**{section:[dict(group,net_paid_minor=group['receipts_minor']+group['refunds_minor']) for key,group in sorted(groups.items())] for section,groups in grouped.items()})


def _csv_safe(value):
    text = ''.join(char if unicodedata.category(char) not in ('Cc','Cf','Cs') else ' ' for char in str(value or ''))
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text


def accounting_entries(rows):
    """Paid accounting evidence only; recovery records remain in the ledger."""
    return [row for row in rows if row['status'] == 'ready'
            and not row['duplicate_of'] and row['document_type'] in ('receipt', 'refund')
            and row['currency'] in UNITS and type(row['amount_minor']) is int]


def export_csv(business_key=None, year=None, rows=None):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['entry_id','business','date','date_basis','vendor','document_type','reference','currency','amount','status','review_reasons','duplicate_of','source_folder','message_id','uid','uidvalidity','category','category_suggestion','comment','unconfirmed_amount'])
    for row in (list_entries(business_key, year=year) if rows is None else rows):
        writer.writerow([row['id'],_csv_safe(row['business_key']),row['document_date'] or '',row['date_basis'],_csv_safe(row['vendor']),row['document_type'],_csv_safe(row['reference']),row['currency'] or '',_amount(row['amount_minor'],row['currency']) if row['amount_minor'] is not None and row['currency'] in UNITS else '',row['status'],'; '.join(row['review_reasons']),row['duplicate_of'] or '',_csv_safe(row['mailbox']),_csv_safe(row['message_id']),row['uid'],row['uidvalidity'],_csv_safe(row['category']),_csv_safe(row['category_suggestion']),_csv_safe(row['comment']),_csv_safe(row['amount_candidate'])])
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, help='Load the private Tahor environment file')
    parser.add_argument('--business', help='Filter by the private configured business key')
    parser.add_argument('--csv', type=Path, help='Create a new private CSV file (existing files are never overwritten)')
    args = parser.parse_args()
    os.umask(0o077)
    if args.env:
        from run import load_environment
        load_environment(args.env.expanduser())
    if args.csv:
        descriptor = os.open(args.csv.expanduser(), os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(descriptor,'w',newline='') as stream:
            stream.write(export_csv(args.business))
    else:
        print(json.dumps({'summaries':summaries(args.business),'review_needed':sum(row['status']=='review_needed' for row in list_entries(args.business))},indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
