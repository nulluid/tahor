import csv
import io
import json
import unittest
from tests import test_accounting_dashboard as fixtures
import accounting_dashboard as accounting
import accounting_export
import business_ledger as ledger


class ExportTests(unittest.TestCase):
    setUp = fixtures.AccountingTests.setUp
    receipt = fixtures.AccountingTests.receipt
    def test_scoped_files_and_integer_allocations(self):
        first=self.receipt(); second=self.receipt('second',2)
        accounting.save_entry_details(first,allocations=[{'category':'=unsafe','bps':8000},{'category':'Admin','bps':2000}],asset={'name':'Equipment','basis_minor':1001})
        accounting.save_entry_details(second,notes='Other business secret')
        accounting.upsert_expected('future','first',vendor='Future supplier',year=2026,amount_minor=500,currency='USD')
        files=accounting_export.supplementary('first',2026,[first,second])
        data=json.loads(files['accounting.json'])
        self.assertEqual([e['entry_id'] for e in data['entry_details']],[first])
        self.assertEqual(data['expected_transactions'],[])
        self.assertNotIn('Other business secret',str(files))
        rows=list(csv.DictReader(io.StringIO(files['allocation-lines.csv'])))
        self.assertEqual(sum(int(r['allocated_amount_minor']) for r in rows),1001)
        self.assertEqual(rows[0]['account'],"'=unsafe")
        all_files=accounting_export.supplementary('first',None,[first],include_expected=True)
        self.assertEqual(len(json.loads(all_files['accounting.json'])['expected_transactions']),1)

    def test_removed_and_financing_rows_never_add_allocations(self):
        first=self.receipt();second=self.receipt(uid=2)
        ledger.exclude_entry(first)
        accounting.save_entry_details(second,transaction_role='financing_principal')
        files=accounting_export.supplementary('first',2026,[first,second],include_expected=True)
        self.assertEqual(list(csv.DictReader(io.StringIO(files['allocation-lines.csv']))),[])

    def test_supplied_financial_snapshot_is_not_reread(self):
        from unittest.mock import patch
        first=self.receipt()
        snapshot=ledger.list_entries('first',year=2026)
        ledger.confirm_entry(first,document_type='receipt',currency='USD',amount='99.99',document_date='2026-01-02')
        with patch.object(ledger,'list_entries',side_effect=AssertionError('Financial rows must not be reread')), patch.object(accounting,'entry_details',side_effect=AssertionError('Annotations use one snapshot')):
            files=accounting_export.supplementary('first',2026,[first],rows=snapshot)
        allocations=list(csv.DictReader(io.StringIO(files['allocation-lines.csv'])))
        self.assertEqual(int(allocations[0]['allocated_amount_minor']),1001)
