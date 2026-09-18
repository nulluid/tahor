"""Business-scoped annotations and integer allocations preserve evidence."""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import accounting_dashboard as accounting
import business_ledger as ledger


class AccountingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        p=patch.object(importlib.import_module('tahor_db'),'DB_PATH',Path(temp.name)/'private.db');p.start();self.addCleanup(p.stop)
        accounting.save_profile('first',name='Example Studio',default=True)
        accounting.save_profile('second',name='Example Workshop')

    def receipt(self,business='first',uid=1,amount='10.01'):
        return ledger.record_receipt(dict(business_key=business,matched_rule_id='rule',mailbox='Business',message_id='<%s@example.test>'%uid,uid=str(uid),uidvalidity='1',sender_email='billing@example.test',vendor='Example Cloud',source_date='2026-01-02',received_at='2026-01-02T12:00:00Z',subject='Receipt'), 'Amount paid: USD '+amount,verified_business=True)

    def test_defaults_and_profiles_are_private_runtime_data(self):
        self.assertEqual(accounting.default_business(),'first')
        accounting.save_profile('second',default=True)
        self.assertEqual(accounting.default_business(),'second')
        self.assertEqual(sum(bool(p.get('default')) for p in accounting.list_profiles()),1)

    def test_exact_split_and_credit_symmetry(self):
        parts=[dict(category='Development',bps=8000),dict(category='Administration',bps=2000)]
        self.assertEqual(accounting.split_minor(1001,parts),[801,200])
        self.assertEqual(accounting.split_minor(-1001,parts),[-801,-200])
        with self.assertRaises(ValueError): accounting.split_minor(1001,[dict(category='Bad',bps=9000)])
        identifier=self.receipt()
        accounting.save_entry_details(identifier,allocations=parts,business_use_bps=9000,tax_treatment='Review required')
        report=accounting.dashboard('first',2026)
        self.assertEqual(report['cash_totals'][0]['amount_minor'],1001)
        self.assertEqual(sum(r['amount_minor'] for r in report['provisional_tax_buckets']),901)

    def test_expected_records_never_enter_totals_and_require_same_business_evidence(self):
        first=self.receipt();second=self.receipt('second',2)
        accounting.upsert_expected('purchase','first',date='2026-01-02',vendor='Example Cloud',amount_minor=1001,currency='USD',status='evidence_missing')
        self.assertEqual(accounting.dashboard('first',2026)['cash_totals'][0]['amount_minor'],1001)
        with self.assertRaises(ValueError): accounting.reconcile_expected('purchase',second)
        accounting.reconcile_expected('purchase',first)
        with self.assertRaises(ValueError): accounting.reassign_entry(first,'second')

    def test_reassignment_rekeys_source_and_preserves_evidence(self):
        identifier=self.receipt()
        old=ledger.get_entry(identifier)
        accounting.reassign_entry(identifier,'second')
        new=ledger.get_entry(identifier)
        self.assertNotEqual(old['source_key'],new['source_key'])
        self.assertEqual(old['source_digest'],new['source_digest'])
        self.assertEqual(accounting.dashboard('first',2026)['entries'],[])
        self.assertEqual(accounting.dashboard('second',2026)['entries'][0]['id'],identifier)
        self.assertEqual(self.receipt('second'),identifier)

    def test_related_transactions_and_financing_exclusion(self):
        first=self.receipt();second=self.receipt('second',2)
        with self.assertRaises(ValueError): accounting.save_entry_details(first,related_entry_id=second)
        accounting.save_entry_details(first,transaction_role='financing_principal')
        self.assertEqual(accounting.dashboard('first',2026)['cash_totals'],[])
        self.assertEqual(accounting.eligible_entries(ledger.list_entries('first')),[])

    def test_invalid_dates_allocations_and_unknown_fields_leave_no_annotations(self):
        identifier=self.receipt()
        for fields in ({'business_use_bps':10001},{'asset':{'placed_in_service_date':'2026-02-30'}},{'oops':1},{'allocations':[{'category':'Dev','bps':True}]}):
            with self.subTest(fields=fields),self.assertRaises(ValueError): accounting.save_entry_details(identifier,**fields)
        self.assertEqual(accounting.entry_details(identifier),{})

    def test_private_policy_only_fills_new_annotations_and_keeps_owner_edits(self):
        accounting.save_profile('first',automation_rules=[{'aliases':['Example Cloud'],'currency':'USD','details':{'tax_treatment':'Review required','allocations':[{'category':'Development','bps':10000}]},'comment':'Application testing','category':'Development'}])
        identifier=self.receipt()
        self.assertEqual(accounting.apply_pending(),1)
        accounting.save_entry_details(identifier,notes='Owner correction')
        self.assertEqual(accounting.apply_pending(),0)
        self.assertEqual(accounting.entry_details(identifier)['notes'],'Owner correction')
        self.assertEqual(ledger.get_entry(identifier)['comment'],'Application testing')

    def test_export_scope_omits_other_business_and_unselected_entries(self):
        first=self.receipt(); second=self.receipt(uid=2);foreign=self.receipt('second',3)
        accounting.save_entry_details(first,notes='First note')
        accounting.save_entry_details(second,notes='Not selected')
        accounting.save_entry_details(foreign,notes='Other business')
        result=accounting.export_data('first',2026,entry_ids=[first])
        self.assertEqual([e['id'] for e in result['entries']],[first])
        self.assertNotIn('Not selected',str(result));self.assertNotIn('Other business',str(result))
        self.assertNotIn('cash_totals',result)

    def test_changed_expected_evidence_is_not_presented_as_matched(self):
        identifier=self.receipt()
        accounting.upsert_expected('expected','first',date='2026-01-02',amount_minor=1001,currency='USD',entry_id=identifier)
        ledger.exclude_entry(identifier)
        row=accounting.list_expected('first',2026)[0]
        self.assertEqual(row['status'],'evidence_missing')
        self.assertTrue(row['needs_reconciliation'])
        self.assertEqual(row['entry_id'],identifier)

    def test_reassignment_rejects_duplicate_relationships(self):
        first=self.receipt()
        original=ledger.get_entry(first)
        db=ledger._db()
        try:
            with db:
                db.execute('UPDATE business_ledger SET duplicate_of=? WHERE id=?',(999,first))
        finally: db.close()
        with self.assertRaises(ValueError): accounting.reassign_entry(first,'second')
        self.assertEqual(ledger.get_entry(first)['business_key'],'first')

    def test_trade_names_are_private_validated_and_deduplicated(self):
        accounting.save_profile('first',trade_names=['Example Products','example products','Other Brand'])
        profile=next(p for p in accounting.list_profiles() if p['business_key']=='first')
        self.assertEqual(profile['trade_names'],['Example Products','Other Brand'])
        self.assertEqual(accounting.export_data('first',2026)['profile']['trade_names'],profile['trade_names'])
        for invalid in ('Brand', [''], ['x'*201], ['Brand']*31):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                accounting.save_profile('first',trade_names=invalid)
        other=next(p for p in accounting.list_profiles() if p['business_key']=='second')
        self.assertNotIn('trade_names',other)
