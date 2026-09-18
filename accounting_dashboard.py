"""Private business bookkeeping annotations; proposed tax mappings are not deductions."""
from datetime import date, datetime, timezone
import json
import hashlib
import re
import business_ledger as ledger


def _db():
    db = ledger._db()
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS accounting_profiles (business_key TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS accounting_details (entry_id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS accounting_expected (record_key TEXT PRIMARY KEY, business_key TEXT NOT NULL, data TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS accounting_audit (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, record_key TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, changed_at TEXT NOT NULL)')
    return db


def _text(value, limit=4000):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('Invalid text')
    return value.strip()


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', value):
        raise ValueError('Invalid business or record key')
    return value


def _date(value):
    if not value: return ''
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Use an ISO calendar date')
    return value


def _integer(value, low=0, high=10**14):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid integer amount or percentage')
    return value


def _audit(db, kind, key, before, after):
    db.execute('INSERT INTO accounting_audit(kind,record_key,before_json,after_json,changed_at) VALUES(?,?,?,?,?)',
               (kind, str(key), json.dumps(before), json.dumps(after), datetime.now(timezone.utc).isoformat()))


def list_profiles():
    db = _db()
    try:
        profiles = [dict(json.loads(row['data']), business_key=row['business_key'], key=row['business_key']) for row in db.execute('SELECT * FROM accounting_profiles ORDER BY business_key')]
        known = {p['business_key'] for p in profiles}
        profiles.extend(dict(business_key=r['business_key'],key=r['business_key'],name=r['business_key']) for r in db.execute('SELECT DISTINCT business_key FROM business_ledger ORDER BY business_key') if r['business_key'] not in known)
        return profiles
    finally: db.close()


def default_business():
    profiles = list_profiles()
    return next((p['business_key'] for p in profiles if p.get('default')), profiles[0]['business_key'] if profiles else None)


def save_profile(business_key, **fields):
    _key(business_key)
    allowed = {'name','folder_root','commencement_date','policies','guidance','default','tax_year','entity_type','automation_rules','trade_names'}
    if set(fields) - allowed: raise ValueError('Unknown profile field')
    for key in fields:
        if key == 'trade_names':
            if not isinstance(fields[key], list) or len(fields[key]) > 30:
                raise ValueError('Provide at most 30 trade names')
            names=[]; seen=set()
            for value in fields[key]:
                name=_text(value,200)
                if not name: raise ValueError('Trade names cannot be blank')
                if name.casefold() not in seen:
                    names.append(name); seen.add(name.casefold())
            fields[key]=names
        elif key == 'automation_rules':
            if not isinstance(fields[key],list) or len(fields[key])>100: raise ValueError('Invalid automation rules')
            for rule in fields[key]:
                if not isinstance(rule,dict) or set(rule)-{'aliases','since','amount_minor','currency','details','category','comment'}: raise ValueError('Invalid automation rule')
                if not isinstance(rule.get('aliases'),list) or not rule['aliases'] or len(rule['aliases'])>30: raise ValueError('Vendor aliases required')
                for alias in rule['aliases']: _text(alias,500)
                if 'since' in rule: _date(rule['since'])
                if 'amount_minor' in rule: _integer(rule['amount_minor'])
                if 'currency' in rule and rule['currency'] not in ledger.UNITS: raise ValueError('Unsupported currency')
                _validate_details(dict(rule.get('details',{})))
                if 'business_key' in rule.get('details',{}): raise ValueError('Automation cannot change business')
                for field in ('category','comment'):
                    if field in rule: _text(rule[field],4000)
        elif key == 'default':
            if type(fields[key]) is not bool: raise ValueError('Invalid default flag')
        elif key == 'tax_year': fields[key] = _integer(fields[key],1900,9999)
        elif key == 'commencement_date': fields[key] = _date(fields[key])
        else: fields[key] = _text(fields[key], 40000 if key in ('policies','guidance') else 500)
    db = _db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM accounting_profiles WHERE business_key=?',(business_key,)).fetchone()
            before = json.loads(row['data']) if row else {}
            after = dict(before, **fields)
            if not after.get('name'): after['name']=business_key
            if after.get('default'):
                for other in db.execute('SELECT * FROM accounting_profiles WHERE business_key<>?',(business_key,)).fetchall():
                    old = json.loads(other['data'])
                    if old.get('default'):
                        new = dict(old, default=False)
                        db.execute('UPDATE accounting_profiles SET data=? WHERE business_key=?',(json.dumps(new),other['business_key']))
                        _audit(db,'profile',other['business_key'],old,new)
            db.execute('INSERT OR REPLACE INTO accounting_profiles VALUES(?,?)',(business_key,json.dumps(after)))
            _audit(db,'profile',business_key,before,after)
        return dict(after,business_key=business_key)
    finally: db.close()


def entry_details(identifier):
    ledger.get_entry(identifier)
    db = _db()
    try:
        row = db.execute('SELECT data FROM accounting_details WHERE entry_id=?',(identifier,)).fetchone()
        return json.loads(row['data']) if row else {}
    finally: db.close()


def _validate_details(fields):
    allowed = {'business_key','tax_treatment','tax_description','tax_form','business_use_bps','allocations','asset','evidence_status','notes','related_entry_id','transaction_role','payment_date','service_period_start','service_period_end','prepaid_balance_minor','amortization_start_date','amortization_months'}
    if set(fields)-allowed: raise ValueError('Unknown accounting field')
    for key in fields:
        value = fields[key]
        if key == 'business_key': fields[key] = _key(value)
        elif key in ('payment_date','service_period_start','service_period_end','amortization_start_date'): fields[key]=_date(value)
        elif key == 'prepaid_balance_minor': fields[key]=_integer(value)
        elif key == 'amortization_months': fields[key]=_integer(value,1,1200)
        elif key == 'business_use_bps': fields[key] = _integer(value,0,10000)
        elif key == 'related_entry_id': fields[key] = None if value is None else _integer(value,1)
        elif key == 'allocations':
            if not isinstance(value,list) or len(value)>30: raise ValueError('Invalid allocations')
            for item in value:
                if not isinstance(item,dict) or set(item)-{'category','bps','tax_treatment','tax_description','tax_form'}: raise ValueError('Invalid allocation')
                if not _text(item.get('category',''),200): raise ValueError('Allocation account required')
                _integer(item.get('bps'),1,10000)
                for name in set(item)-{'bps'}: _text(item[name],1000)
            if value and sum(item['bps'] for item in value)!=10000: raise ValueError('Allocations must total 100%')
        elif key == 'asset':
            if not isinstance(value,dict) or set(value)-{'name','purchase_date','placed_in_service_date','basis_minor','serial_number','depreciation_method','remaining_basis_minor','proposed_depreciation_minor'}: raise ValueError('Invalid asset fields')
            for name,item in value.items():
                if name.endswith('_date'): _date(item)
                elif name.endswith('_minor'): _integer(item)
                else: _text(item,1000)
            if value.get('purchase_date') and value.get('placed_in_service_date') and value['placed_in_service_date']<value['purchase_date']: raise ValueError('Service date precedes purchase')
        elif key == 'transaction_role':
            if value not in ('expense','asset','refund','financing_principal','transfer','non_business'): raise ValueError('Invalid transaction role')
        else: fields[key] = _text(value,12000 if key=='notes' else 2000)
    return fields


def save_entry_details(identifier, **fields):
    fields = _validate_details(fields)
    db = _db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            entry = db.execute('SELECT * FROM business_ledger WHERE id=?',(identifier,)).fetchone()
            if not entry: raise LookupError('Expense not found')
            business = fields.get('business_key',entry['business_key'])
            if not db.execute('SELECT 1 FROM accounting_profiles WHERE business_key=?',(business,)).fetchone(): raise ValueError('Unknown business')
            related = fields.get('related_entry_id')
            if related is not None:
                target = db.execute('SELECT * FROM business_ledger WHERE id=?',(related,)).fetchone()
                if not target or related==identifier or target['business_key']!=business or target['currency']!=entry['currency']: raise ValueError('Related transaction must belong to same business and currency')
            if business != entry['business_key']:
                if entry['duplicate_of'] or db.execute('SELECT 1 FROM business_ledger WHERE duplicate_of=?',(identifier,)).fetchone():
                    raise ValueError('Resolve duplicate evidence before reassigning business')
                existing_details=db.execute('SELECT data FROM accounting_details WHERE entry_id=?',(identifier,)).fetchone()
                if existing_details and json.loads(existing_details['data']).get('related_entry_id'):
                    raise ValueError('Unlink related transaction before reassigning business')
                if db.execute("SELECT 1 FROM accounting_details WHERE json_extract(data,'$.related_entry_id')=?",(identifier,)).fetchone():
                    raise ValueError('Unlink related transaction before reassigning business')
                if db.execute("SELECT 1 FROM accounting_expected WHERE json_extract(data,'$.entry_id')=?",(identifier,)).fetchone(): raise ValueError('Unlink expected transaction before reassigning business')
                new_key=hashlib.sha256(json.dumps([business,entry['sender_email'],entry['message_id']]).encode()).hexdigest()
                if db.execute('SELECT 1 FROM business_ledger WHERE source_key=? AND id<>?',(new_key,identifier)).fetchone(): raise ValueError('Target business already has this evidence')
                db.execute('UPDATE business_ledger SET business_key=?,source_key=?,updated_at=? WHERE id=?',(business,new_key,datetime.now(timezone.utc).isoformat(),identifier))
                ledger._audit(db,dict(entry))
            row = db.execute('SELECT data FROM accounting_details WHERE entry_id=?',(identifier,)).fetchone()
            before = json.loads(row['data']) if row else {}
            after = dict(before, **fields)
            after['business_key'] = business
            if after.get('service_period_start') and after.get('service_period_end') and after['service_period_end']<after['service_period_start']: raise ValueError('Service period ends before it begins')
            db.execute('INSERT OR REPLACE INTO accounting_details VALUES(?,?)',(identifier,json.dumps(after)))
            _audit(db,'entry',identifier,before,after)
        return after
    finally: db.close()


def list_expected(business_key, year=None):
    db = _db()
    try:
        rows=[dict(json.loads(r['data']),record_key=r['record_key'],business_key=r['business_key']) for r in db.execute('SELECT * FROM accounting_expected WHERE business_key=? ORDER BY record_key',(business_key,))]
        for row in rows:
            if row.get('status')=='matched':
                target=db.execute('SELECT * FROM business_ledger WHERE id=?',(row.get('entry_id'),)).fetchone()
                valid=bool(target and target['business_key']==business_key and target['status']!='excluded' and not target['duplicate_of'] and target['document_type'] in ('receipt','refund'))
                if valid and row.get('currency'): valid=row['currency']==target['currency']
                if valid and row.get('amount_minor') is not None: valid=row['amount_minor']==target['amount_minor']
                if not valid:
                    row['status']='evidence_missing'; row['needs_reconciliation']=True
                    row['reconciliation_note']='Linked evidence has changed; review and reconcile this expectation again.'
        return [r for r in rows if year is None or str(r.get('date','')).startswith(str(int(year))+'-') or r.get('year')==int(year)]
    finally: db.close()


def upsert_expected(record_key, business_key, **fields):
    _key(record_key); _key(business_key)
    if set(fields)-{'vendor','date','year','amount_minor','currency','purpose','category','notes','status','entry_id'}: raise ValueError('Unknown expected transaction field')
    for key,value in fields.items():
        if key in ('amount_minor','entry_id'): fields[key]=None if value is None else _integer(value, -10**14 if key=='amount_minor' else 1)
        elif key=='year': fields[key]=_integer(value,1900,9999)
        elif key=='date': fields[key]=_date(value)
        elif key=='currency':
            if value not in ledger.UNITS: raise ValueError('Unsupported currency')
        elif key=='status':
            if value not in ('expected','evidence_missing','matched','cancelled'): raise ValueError('Invalid expected status')
        else: fields[key]=_text(value,12000)
    db=_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM accounting_profiles WHERE business_key=?',(business_key,)).fetchone(): raise ValueError('Unknown business')
            row=db.execute('SELECT * FROM accounting_expected WHERE record_key=?',(record_key,)).fetchone()
            if row and row['business_key']!=business_key: raise ValueError('Expected record belongs to another business')
            before=json.loads(row['data']) if row else {}
            after=dict(before,**fields)
            if after.get('entry_id'):
                target=db.execute('SELECT * FROM business_ledger WHERE id=?',(after['entry_id'],)).fetchone()
                if not target or target['business_key']!=business_key: raise ValueError('Expected record requires same-business expense')
                if target['status']=='excluded' or target['document_type'] not in ('receipt','refund'): raise ValueError('Expected record requires receipt evidence')
                if after.get('currency') and after['currency']!=target['currency']: raise ValueError('Currency mismatch')
                if after.get('amount_minor') is not None and after['amount_minor']!=target['amount_minor']: raise ValueError('Amount mismatch')
                if db.execute("SELECT 1 FROM accounting_expected WHERE record_key<>? AND json_extract(data,'$.entry_id')=?",(record_key,after['entry_id'])).fetchone(): raise ValueError('Receipt already reconciles another expected transaction')
                after['status']='matched'
            elif after.get('status')=='matched': raise ValueError('Matched record requires receipt')
            db.execute('INSERT OR REPLACE INTO accounting_expected VALUES(?,?,?)',(record_key,business_key,json.dumps(after)))
            _audit(db,'expected',record_key,before,after)
        return dict(after,record_key=record_key,business_key=business_key)
    finally: db.close()


def reconcile_expected(record_key, entry_id):
    db=_db()
    try: row=db.execute('SELECT business_key FROM accounting_expected WHERE record_key=?',(record_key,)).fetchone()
    finally: db.close()
    if not row: raise LookupError('Expected transaction not found')
    return upsert_expected(record_key,row['business_key'],entry_id=entry_id)


def split_minor(amount, allocations):
    """Largest-remainder integer allocation; credits exactly reverse charges."""
    _integer(amount,-10**14)
    _validate_details({'allocations':allocations})
    if not allocations: return []
    absolute=abs(amount)
    values=[absolute*a['bps']//10000 for a in allocations]
    remainder=absolute-sum(values)
    order=sorted(range(len(values)),key=lambda i:-(absolute*allocations[i]['bps']%10000))
    for i in order[:remainder]: values[i]+=1
    return [value if amount>=0 else -value for value in values]


def dashboard(business_key, year):
    _key(business_key); _integer(year,1900,9999)
    profile=next((p for p in list_profiles() if p['business_key']==business_key),None)
    if not profile: raise LookupError('Business not found')
    entries=[e for e in ledger.list_entries(business_key,year=year) if e['status']!='excluded' and e['document_type'] in ('receipt','refund')]
    db=_db()
    try: annotations={r['entry_id']:json.loads(r['data']) for r in db.execute('SELECT * FROM accounting_details')}
    finally: db.close()
    totals={}; buckets={}
    for entry in entries:
        details=annotations.get(entry['id'],{})
        entry['accounting']=details
        if not ledger.accounting_entries([entry]) or details.get('transaction_role') in ('financing_principal','transfer','non_business'): continue
        currency=entry['currency']; amount=entry['amount_minor']
        totals[currency]=totals.get(currency,0)+amount
        percentage=details.get('business_use_bps',10000)
        business_amount=(abs(amount)*percentage+5000)//10000*(1 if amount>=0 else -1)
        allocations=details.get('allocations') or [{'category':entry.get('category') or 'Unclassified','bps':10000}]
        for allocation,value in zip(allocations,split_minor(business_amount,allocations)):
            key=(currency,allocation['category'],allocation.get('tax_treatment',details.get('tax_treatment','Needs review')),allocation.get('tax_form',details.get('tax_form','')))
            buckets[key]=buckets.get(key,0)+value
    return {'profile':profile,'business_key':business_key,'year':year,'entries':entries,'expected':list_expected(business_key,year),
            'cash_totals':[{'currency':c,'amount_minor':v} for c,v in sorted(totals.items())],
            'provisional_tax_buckets':[dict(currency=k[0],category=k[1],tax_treatment=k[2],tax_form=k[3],amount_minor=v) for k,v in sorted(buckets.items())],
            'tax_notice':'Proposed classifications and allocated spending, not calculated tax deductions. Review eligibility, commencement and service dates, and final tax forms.'}


def export_data(business_key,year,entry_ids=None):
    data=dashboard(business_key,year)
    if entry_ids is not None:
        selected=set(entry_ids)
        data['entries']=[e for e in data['entries'] if e['id'] in selected]
        data['expected']=[r for r in data['expected'] if r.get('entry_id') in selected]
        # Totals for a filtered export must never silently describe omitted rows.
        data.pop('cash_totals',None); data.pop('provisional_tax_buckets',None)
    ids={str(e['id']) for e in data['entries']}; expected={r['record_key'] for r in data['expected']}
    db=_db()
    try:
        data['audit']=[dict(r) for r in db.execute('SELECT * FROM accounting_audit ORDER BY id') if (r['kind']=='profile' and r['record_key']==business_key) or (r['kind']=='entry' and r['record_key'] in ids) or (r['kind']=='expected' and r['record_key'] in expected)]
    finally: db.close()
    return data


def reassign_entry(identifier, business_key):
    return save_entry_details(identifier,business_key=business_key)


def apply_pending(limit=100):
    """Apply owner-authored private policies to unannotated evidence only."""
    count=0
    for profile in list_profiles():
        for entry in ledger.list_entries(profile['business_key']):
            if count>=limit: return count
            if entry['status']=='excluded' or entry['document_type'] not in ('receipt','refund') or entry_details(entry['id']): continue
            matches=[]
            for rule in profile.get('automation_rules',[]):
                if entry['vendor'].casefold() not in [a.casefold() for a in rule['aliases']]: continue
                if rule.get('since') and (entry.get('document_date') or '')<rule['since']: continue
                if 'amount_minor' in rule and entry['amount_minor']!=rule['amount_minor']: continue
                if rule.get('currency') and entry['currency']!=rule['currency']: continue
                matches.append(rule)
            if len(matches)!=1: continue
            rule=matches[0]
            save_entry_details(entry['id'],**rule.get('details',{}))
            if not entry.get('metadata_confirmed'):
                ledger.update_metadata(entry['id'],comment=entry.get('comment') or rule.get('comment',''),category=entry.get('category') or rule.get('category',''))
            count+=1
    return count


def eligible_entries(rows):
    return [r for r in ledger.accounting_entries(rows) if entry_details(r['id']).get('transaction_role') not in ('financing_principal','transfer','non_business')]


def allocated_entries(rows):
    """Reporting-only split rows; never insert these projections in the ledger."""
    result=[]
    for row in eligible_entries(rows):
        details=entry_details(row['id']);amount=row['amount_minor']
        business_amount=(abs(amount)*details.get('business_use_bps',10000)+5000)//10000*(1 if amount>=0 else -1)
        parts=details.get('allocations') or [{'category':row.get('category') or 'Unclassified','bps':10000}]
        for part,value in zip(parts,split_minor(business_amount,parts)):
            result.append(dict(row,amount_minor=value,category=part['category']))
    return result
