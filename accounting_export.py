"""Business-scoped tax preparation evidence, not a calculated tax return."""
import csv
import io
import json
import accounting_dashboard as accounting
import business_ledger as ledger


def _csv(headers, rows):
    stream=io.StringIO(newline='')
    writer=csv.writer(stream); writer.writerow(headers)
    for row in rows:
        writer.writerow([value if type(value) is int else ledger._csv_safe(value) for value in row])
    return stream.getvalue()


def supplementary(business_key, year, entry_ids, include_expected=False, rows=None):
    """Use one selected financial snapshot and one annotation snapshot per export."""
    selected=set(entry_ids)
    source_rows=ledger.list_entries(business_key,year=year) if rows is None else rows
    rows=[dict(r) for r in source_rows if r['id'] in selected and r['business_key']==business_key
          and (year is None or str(r.get('document_date') or r.get('received_at') or '').startswith(str(int(year))+'-'))]
    db=accounting._db()
    try:
        db.execute('BEGIN')
        stored=db.execute('SELECT data FROM accounting_profiles WHERE business_key=?',(business_key,)).fetchone()
        if stored:
            profile=dict(json.loads(stored['data']),business_key=business_key,key=business_key)
        elif rows or db.execute('SELECT 1 FROM business_ledger WHERE business_key=?',(business_key,)).fetchone():
            profile=dict(business_key=business_key,key=business_key,name=business_key)
        else: raise LookupError('Business not found')
        annotations={r['entry_id']:json.loads(r['data']) for r in db.execute('SELECT * FROM accounting_details') if r['entry_id'] in selected}
        expected=[]
        if include_expected:
            for record in db.execute('SELECT * FROM accounting_expected WHERE business_key=? ORDER BY record_key',(business_key,)):
                item=dict(json.loads(record['data']),record_key=record['record_key'],business_key=business_key)
                if year is not None and not str(item.get('date','')).startswith(str(int(year))+'-') and item.get('year')!=int(year): continue
                if item.get('status')=='matched':
                    target=db.execute('SELECT * FROM business_ledger WHERE id=?',(item.get('entry_id'),)).fetchone()
                    valid=bool(target and target['business_key']==business_key and target['status']!='excluded' and not target['duplicate_of'] and target['document_type'] in ('receipt','refund'))
                    if valid and item.get('currency'): valid=item['currency']==target['currency']
                    if valid and item.get('amount_minor') is not None: valid=item['amount_minor']==target['amount_minor']
                    if not valid:
                        item.update(status='evidence_missing',needs_reconciliation=True,reconciliation_note='Linked evidence has changed; review and reconcile this expectation again.')
                expected.append(item)
        expected_keys={r['record_key'] for r in expected}; ids={str(r['id']) for r in rows}
        audit=[dict(r) for r in db.execute('SELECT * FROM accounting_audit ORDER BY id') if (r['kind']=='entry' and r['record_key'] in ids) or (r['kind']=='expected' and r['record_key'] in expected_keys) or (r['kind']=='profile' and r['record_key']==business_key)]
    finally: db.close()
    details=[]; allocations=[]; assets=[]
    for row in rows:
        detail=annotations.get(row['id'],{})
        details.append({'entry_id':row['id'],'accounting':detail})
        if not ledger.accounting_entries([row]) or detail.get('transaction_role') in ('financing_principal','transfer','non_business'): continue
        amount=row['amount_minor']; bps=detail.get('business_use_bps',10000)
        allocated_amount=(abs(amount)*bps+5000)//10000*(1 if amount>=0 else -1)
        parts=detail.get('allocations') or [{'category':row.get('category') or 'Unclassified','bps':10000}]
        for part,value in zip(parts,accounting.split_minor(allocated_amount,parts)):
            allocations.append([row['id'],row['document_date'],row['vendor'],row['document_type'],row['currency'],part['category'],part['bps'],bps,value,ledger._amount(value,row['currency']),part.get('tax_treatment',detail.get('tax_treatment','Needs review')),part.get('tax_form',detail.get('tax_form','')),part.get('tax_description',detail.get('tax_description','')),detail.get('payment_date',''),detail.get('service_period_start',''),detail.get('service_period_end',''),detail.get('prepaid_balance_minor','')])
        asset=detail.get('asset',{})
        if asset or detail.get('amortization_months'):
            assets.append([row['id'],row['vendor'],row['currency'],asset.get('name',''),asset.get('purchase_date',''),asset.get('placed_in_service_date',''),asset.get('basis_minor',''),bps,asset.get('depreciation_method',''),asset.get('proposed_depreciation_minor',''),asset.get('remaining_basis_minor',''),detail.get('amortization_start_date',''),detail.get('amortization_months',''),asset.get('serial_number','')])
    document={'business_key':business_key,'year':year,'profile':profile,'entry_details':details,'expected_transactions':expected,'audit':audit,'notice':'Allocation amounts are classified business spending, not allowed tax deductions. Asset basis and proposed depreciation are separate evidence fields; do not add them to expense amounts. Expected transactions are not paid expenses.'}
    return {
        'accounting.json':json.dumps(document,indent=2,ensure_ascii=False),
        'allocation-lines.csv':_csv(['entry_id','date','vendor','document_type','currency','account','allocation_bps','business_use_bps','allocated_amount_minor','allocated_amount','provisional_tax_treatment','provisional_tax_form','provisional_tax_description','payment_date','service_period_start','service_period_end','prepaid_balance_minor'],allocations),
        'assets.csv':_csv(['entry_id','vendor','currency','asset','purchase_date','placed_in_service_date','basis_minor','business_use_bps','proposed_method','proposed_depreciation_minor','remaining_basis_minor','amortization_start_date','amortization_months','serial_number'],assets),
    }
