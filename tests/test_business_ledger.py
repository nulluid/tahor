"""Business evidence stays private and ambiguous amounts never enter totals."""
import csv
import importlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import business_ledger as ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        patched=patch.object(importlib.import_module('tahor_db'),'DB_PATH',Path(self.temp.name)/'private.db')
        patched.start();self.addCleanup(patched.stop)
        self.metadata=dict(business_key='example-services',matched_rule_id='rule-1',mailbox='INBOX',message_id='<one@example.test>',uid='7',uidvalidity='42',sender_email='billing@vendor.example',vendor='Example vendor',source_date='2026-01-02',received_at='2026-01-02T12:00:00+00:00',subject='Your receipt')
        self.receipt='Amount paid: USD 123.45\nPayment date: 2026-01-02\nReceipt ID: ABC-123'

    def add(self,text=None,**metadata):
        return ledger.record_receipt(dict(self.metadata,**metadata),self.receipt if text is None else text,verified_business=True)

    def test_guard_rejects_unverified_sources_and_invalid_identity(self):
        with self.assertRaises(ValueError):ledger.record_receipt(self.metadata,self.receipt)
        for changes in ({'matched_rule_id':''},{'uid':'7\r\nSEARCH ALL'},{'uidvalidity':'0'},{'uid':'4294967296'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.add(**changes)

    def test_exact_paid_amount_and_separate_invoice_refund_totals(self):
        self.add()
        self.add('Invoice\nAmount due: USD 200.00\nInvoice ID: INV-1',message_id='<invoice@example.test>',subject='Invoice')
        self.add('Refund processed\nRefund total: USD 20.00\nRefund ID: RF-1',message_id='<refund@example.test>',subject='Refund')
        self.assertEqual(ledger.summaries(),[dict(business_key='example-services',currency='USD',receipts_minor=12345,refunds_minor=-2000,invoices_minor=20000,net_paid_minor=10345)])
        self.assertTrue(all(row['status']=='ready' for row in ledger.list_entries()))

    def test_currencies_never_convert_or_round(self):
        self.add('Amount paid: EUR 1,234.56',message_id='<eur@example.test>')
        self.add('Amount paid: JPY 2500',message_id='<jpy@example.test>')
        self.add('Amount paid: KWD 1.234',message_id='<kwd@example.test>')
        groups={row['currency']:row for row in ledger.summaries()}
        self.assertEqual(groups['EUR']['net_paid_minor'],123456)
        self.assertEqual(groups['JPY']['net_paid_minor'],2500)
        self.assertEqual(groups['KWD']['net_paid_minor'],1234)
        for value in ('USD 1.234','EUR 1.234,56','USD €12.00','JPY $20','USD 20 and USD 30','$12.00'):
            with self.subTest(value=value):
                result=ledger.extract('Amount paid: '+value,source_date='2026-01-02')
                self.assertEqual(result['status'],'review_needed');self.assertIsNone(result['amount_minor'])

    def test_duplicate_source_and_reference_do_not_double_count(self):
        first=self.add();again=self.add(mailbox='Business/2026/Vendor',uid='80',uidvalidity='99')
        self.assertEqual(first,again);self.assertEqual(len(ledger.list_entries()),1)
        duplicate=self.add(message_id='<other@example.test>')
        row=ledger.get_entry(duplicate)
        self.assertEqual(row['duplicate_of'],first);self.assertEqual(row['status'],'review_needed')
        self.assertEqual(ledger.summaries()[0]['net_paid_minor'],12345)
        with self.assertRaises(ValueError):ledger.confirm_entry(duplicate,document_type='receipt',currency='USD',amount='123.45',document_date='2026-01-02')
        ledger.exclude_entry(duplicate)
        self.assertEqual(ledger.get_entry(duplicate)['status'],'excluded')
        self.assertEqual(ledger.summaries()[0]['net_paid_minor'],12345)

    def test_conflicting_message_id_preserves_source_and_blocks_confirmation(self):
        first=self.add()
        self.assertEqual(self.add('Amount paid: USD 999.00',mailbox='Other',uid='90'),first)
        row=ledger.get_entry(first)
        self.assertEqual((row['mailbox'],row['uid'],row['amount_minor']),('INBOX','7',12345))
        self.assertIn('source_identity_collision',row['review_reasons'])
        self.assertEqual(ledger.summaries(),[])
        self.add(mailbox='Another',uid='91')
        self.assertEqual(ledger.get_entry(first)['mailbox'],'INBOX')
        for distinct in (False,True):
            with self.assertRaises(ValueError):
                ledger.confirm_entry(first,document_type='receipt',currency='USD',amount='123.45',document_date='2026-01-02',distinct_document=distinct)
        ledger.exclude_entry(first)
        self.assertEqual(ledger.get_entry(first)['status'],'excluded')

    def test_identical_body_without_reference_requires_duplicate_review(self):
        first=self.add('Amount paid: USD 10.00')
        second=self.add('Amount paid: USD 10.00',message_id='<second@example.test>')
        self.assertEqual(ledger.get_entry(second)['duplicate_of'],first)
        ledger.confirm_entry(second,document_type='receipt',currency='USD',amount='10.00',document_date='2026-01-02',distinct_document=True)
        self.assertEqual(ledger.summaries()[0]['net_paid_minor'],2000)

    def test_invoice_and_paid_receipt_share_reference_but_stay_separate(self):
        self.add('Invoice\nInvoice ID: INV-8\nAmount due: USD 90.00',subject='Invoice')
        self.add('Payment received\nInvoice ID: INV-8\nAmount paid: USD 90.00',message_id='<paid@example.test>')
        result=ledger.summaries()[0]
        self.assertEqual(result['invoices_minor'],9000);self.assertEqual(result['net_paid_minor'],9000)

    def test_missing_or_conflicting_evidence_requires_review(self):
        for text in ('Your receipt\nThanks', 'Amount paid: USD 10.00\nTotal paid: USD 20.00', 'Payment pending\nTotal: USD 10.00', 'Refund requested\nTotal: USD 10.00'):
            with self.subTest(text=text):
                self.assertEqual(ledger.extract(text,source_date='2026-01-02')['status'],'review_needed')
        self.assertEqual(ledger.extract(self.receipt.replace('Payment date: 2026-01-02\n',''))['status'],'review_needed')

    def test_owner_confirmation_is_audited_and_rescan_preserves_it(self):
        identifier=self.add('Your receipt\nTotal: $12.00')
        self.assertEqual(ledger.summaries(),[])
        ledger.confirm_entry(identifier,document_type='receipt',currency='CAD',amount='12.00',document_date='2026-01-02')
        self.add('Your receipt\nTotal: $12.00',uid='9')
        row=ledger.get_entry(identifier)
        self.assertEqual(row['currency'],'CAD');self.assertEqual(row['amount_minor'],1200);self.assertEqual(row['uid'],'9')
        db=ledger._db();self.assertEqual(db.execute('SELECT COUNT(*) FROM business_ledger_reviews').fetchone()[0],1);db.close()

    def test_csv_neutralizes_untrusted_cells_and_keeps_refunds_numeric(self):
        self.add('Refund processed\nRefund total: USD -12.00',vendor=' \t=HYPERLINK("https://example.test")',mailbox='+SUM(1,1)',business_key='@SUM(1)')
        rows=list(csv.DictReader(io.StringIO(ledger.export_csv())))
        self.assertTrue(rows[0]['vendor'].startswith("'"));self.assertTrue(rows[0]['source_folder'].startswith("'"));self.assertTrue(rows[0]['business'].startswith("'"))
        self.assertEqual(rows[0]['amount'],'-12.00')
        self.assertNotIn('source_digest',rows[0]);self.assertNotIn('Refund processed',ledger.export_csv())

    def test_unknown_currency_and_invalid_dates_are_not_guessed(self):
        self.assertEqual(ledger.extract('Amount paid: XYZ 10',source_date='2026-01-02')['status'],'review_needed')
        self.assertEqual(ledger.extract('Amount paid: USD 10',source_date='2026-01-02invalid')['status'],'review_needed')
        with self.assertRaises(ValueError):ledger.confirm_entry(self.add(),document_type='receipt',currency='USD',amount='1.00',document_date='2026-01-02garbage')

    def test_private_export_never_overwrites_existing_file(self):
        import os
        self.add()
        destination=Path(self.temp.name)/'expenses.csv'
        before=os.umask(0o077)
        try:
            with patch('sys.argv',['business_ledger.py','--csv',str(destination)]):
                self.assertEqual(ledger.main(),0)
                content=destination.read_text()
                self.assertEqual(destination.stat().st_mode & 0o777,0o600)
                with self.assertRaises(FileExistsError):ledger.main()
                self.assertEqual(destination.read_text(),content)
        finally:
            os.umask(before)
        self.assertTrue(ledger._csv_safe('\ufeff=1+1').startswith("'"))
