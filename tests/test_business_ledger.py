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

    def test_existing_schema_migrates_and_metadata_is_audited_separately(self):
        db=importlib.import_module('tahor_db').get_db()
        db.execute(ledger.SCHEMA);db.commit();db.close()
        identifier=self.add()
        ledger.update_metadata(identifier,comment='Discuss with accountant\nAnnual service.',category='Software')
        row=ledger.get_entry(identifier)
        self.assertEqual(row['category'],'Software');self.assertIn('\n',row['comment'])
        self.assertEqual(row['subject'],'Your receipt');self.assertEqual(row['owner_confirmed'],0)
        self.assertEqual(row['amount_minor'],12345)
        self.assertFalse(ledger.set_category_suggestion(identifier,'Hardware','Vendor clue'))
        ledger.update_metadata(identifier,comment='Preserve this note',category='')
        self.assertFalse(ledger.set_category_suggestion(identifier,'Software','Vendor clue',expected_source_digest='stale'))
        self.assertTrue(ledger.set_category_suggestion(identifier,'Software','Vendor clue',expected_source_digest=row['source_digest']))
        row=ledger.get_entry(identifier)
        self.assertEqual(row['category'],'');self.assertEqual(row['category_suggestion'],'Software')
        self.assertEqual(row['comment'],'Preserve this note')
        db=ledger._db();self.assertEqual(db.execute('SELECT COUNT(*) FROM business_ledger_reviews').fetchone()[0],3);db.close()
        for changes in ({'comment':'x'*4001,'category':''},{'comment':'','category':'x'*121},{'comment':'','category':'Bad\x00category'}):
            with self.assertRaises(ValueError):ledger.update_metadata(identifier,**changes)

    def test_year_and_category_reports_keep_refunds_invoices_and_currencies_separate(self):
        paid=self.add()
        ledger.update_metadata(paid,comment='',category='Software')
        refund=self.add('Refund processed\nAmount refunded: USD 20.00',message_id='<refund-year@example.test>',source_date='2026-02-03')
        ledger.update_metadata(refund,comment='',category='Software')
        self.add('Invoice\nAmount due: USD 500.00',message_id='<due-year@example.test>',subject='Invoice')
        self.add('Amount paid: EUR 10.00',message_id='<eur-year@example.test>')
        self.add('Amount paid: USD 99.00',message_id='<last-year@example.test>',source_date='2025-12-31')
        self.add('Total: $12.00',message_id='<unknown-year@example.test>')
        duplicate=self.add(message_id='<duplicate-year@example.test>')
        self.assertIsNotNone(ledger.get_entry(duplicate)['duplicate_of'])
        report=ledger.report(2026)
        usd=next(item for item in report['totals'] if item['currency']=='USD')
        self.assertEqual((usd['receipts_minor'],usd['refunds_minor'],usd['net_paid_minor'],usd['invoices_minor']),(12345,-2000,10345,50000))
        category=next(item for item in report['categories'] if item['category']=='Software')
        self.assertEqual(category['net_paid_minor'],10345)
        feb=next(item for item in report['months'] if item['month']=='2026-02')
        self.assertEqual(feb['net_paid_minor'],-2000)
        self.assertTrue(any(item['currency']=='EUR' for item in report['monthly_categories']))
        self.assertEqual(ledger.summaries(year=2025)[0]['net_paid_minor'],9900)
        self.assertEqual(len(ledger.list_entries(year=2025)),1)
        for year in (True,'2026',0,10000):
            with self.assertRaises(ValueError):ledger.report(year)

    def test_year_filter_uses_received_date_only_when_document_date_missing(self):
        missing=self.add('Your receipt\nPayment date: 2026-99-99\nAmount paid: USD 9.00')
        self.assertIsNone(ledger.get_entry(missing)['document_date'])
        self.assertEqual([row['id'] for row in ledger.list_entries(year=2026)],[missing])
        self.assertEqual(ledger.report(2026)['totals'],[])

    def test_csv_year_and_owner_metadata_are_spreadsheet_safe(self):
        identifier=self.add()
        ledger.update_metadata(identifier,comment='=IMPORTDATA("example")',category='@formula')
        self.add('Amount paid: USD 8.00',message_id='<prior@example.test>',source_date='2025-01-02')
        rows=list(csv.DictReader(io.StringIO(ledger.export_csv(year=2026))))
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['comment'].startswith("'="));self.assertTrue(rows[0]['category'].startswith("'@"))

    def test_flattened_and_multiline_receipt_labels_extract_explicit_currency(self):
        samples=('Receipt from Example Amount paid USD 21.75 Date paid Sep 17, 2026 Summary Amount paid: USD 21.75',
                 'Your receipt\nAmount paid\nUSD 21.75\nThank you',
                 'Payment received Amount paid 21.75 USD Thank you')
        for text in samples:
            with self.subTest(text=text):
                result=ledger.extract(text,source_date='2026-09-17')
                self.assertEqual(result['amount_minor'],2175);self.assertEqual(result['currency'],'USD')
                self.assertEqual(result['status'],'ready')

    def test_bare_currency_symbol_preserves_amount_candidate_without_guessing_currency(self):
        text='Example Receipt #1103-5782 Amount paid $21.75 Date paid Sep 17, 2026, 7:40:09 PM Payment method - Summary Credits : $21.75 - Amount paid : $21.75'
        result=ledger.extract(text,source_date='2026-09-17')
        self.assertEqual(result['amount_candidate'],'21.75');self.assertIsNone(result['currency']);self.assertIsNone(result['amount_minor'])
        self.assertIn('currency_unconfirmed',result['review_reasons']);self.assertNotIn('amount_missing',result['review_reasons'])
        for text in ('Amount paid: $20.00 Total paid: $30.00','Amount paid: USD 20 and USD 30','Amount paid: USD 20.00 or USD 30.00'):
            with self.subTest(text=text):
                result=ledger.extract(text,source_date='2026-09-17')
                self.assertEqual(result['amount_candidate'],'');self.assertIsNone(result['amount_minor'])

    def test_verified_reprocessing_is_audited_preserves_provenance_and_owner_choices(self):
        identifier=self.add('Your receipt\nMissing evidence')
        before=ledger.get_entry(identifier)
        self.assertTrue(ledger.reprocess_entry(identifier,'Amount paid: USD 21.75',subject='Your receipt'))
        row=ledger.get_entry(identifier)
        self.assertEqual(row['source_digest'],before['source_digest']);self.assertEqual(row['amount_minor'],2175)
        ledger.confirm_entry(identifier,document_type='receipt',currency='USD',amount='22.00',document_date='2026-01-02')
        self.assertFalse(ledger.reprocess_entry(identifier,'Amount paid: USD 99.00'))
        self.assertEqual(ledger.get_entry(identifier)['amount_minor'],2200)
        excluded=self.add('Your receipt\nExcluded',message_id='<excluded@example.test>')
        ledger.exclude_entry(excluded)
        self.assertFalse(ledger.reprocess_entry(excluded,'Amount paid: USD 99.00'))
        collision=self.add('Your receipt\nCollision',message_id='<collision@example.test>')
        self.add('Different source',message_id='<collision@example.test>')
        self.assertFalse(ledger.reprocess_entry(collision,'Amount paid: USD 99.00'))
        db=ledger._db();self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM business_ledger_reviews WHERE entry_id=?',(identifier,)).fetchone()[0],2);db.close()

    def test_reprocessing_never_includes_duplicate_documents(self):
        first=self.add()
        duplicate=self.add(message_id='<duplicate-again@example.test>')
        ledger.reprocess_entry(duplicate,self.receipt)
        self.assertEqual(ledger.get_entry(duplicate)['duplicate_of'],first)
        self.assertEqual(ledger.get_entry(duplicate)['status'],'review_needed')
        self.assertEqual(ledger.summaries()[0]['net_paid_minor'],12345)

    def test_payment_reminders_and_store_credit_are_not_paid_expenses(self):
        for text in ('Upcoming payment\nAmount due: USD 12.00','Payment reminder\nInvoice\nTotal due: USD 12.00','Your payment is coming up\nTotal: USD 12.00'):
            with self.subTest(text=text):
                result=ledger.extract(text,source_date='2026-01-02')
                self.assertEqual(result['document_type'],'unknown');self.assertEqual(result['status'],'review_needed')
                self.assertIn('payment_reminder_not_receipt',result['review_reasons'])
        cash=ledger.extract('Refunded to your original payment method\nAmount credited: USD 15.00',source_date='2026-01-02')
        self.assertEqual(cash['document_type'],'refund');self.assertEqual(cash['amount_minor'],-1500)
        store=ledger.extract('Refund processed as store credit\nRefund total: USD 15.00',source_date='2026-01-02')
        self.assertEqual(store['document_type'],'unknown');self.assertEqual(store['status'],'review_needed')
        self.assertFalse(ledger.is_payment_reminder('Payment received','Amount paid: USD 12.00. Next payment scheduled soon.'))

    def test_html_table_totals_and_entities_extract_without_style_or_script_noise(self):
        text='<html><head><style>Amount paid USD 999.00</style></head><body><p>Your receipt</p><table><tr><td>Amount paid</td><td>USD&nbsp;21.75</td></tr></table><script>Amount paid USD 700.00</script></body></html>'
        result=ledger.extract(text,source_date='2026-09-17')
        self.assertEqual(result['amount_minor'],2175);self.assertEqual(result['status'],'ready')

    def test_verified_nonreceipt_exclusion_is_audited_without_owner_confirmation(self):
        identifier=self.add('Payment reminder\nTotal: USD 12.00')
        self.assertTrue(ledger.exclude_non_receipt(identifier))
        row=ledger.get_entry(identifier)
        self.assertEqual(row['status'],'excluded');self.assertEqual(row['owner_confirmed'],0)
        self.assertIn('not_receipt',row['review_reasons'])
        self.assertFalse(ledger.exclude_non_receipt(identifier))
        owner=self.add(message_id='<owner-kept@example.test>')
        ledger.confirm_entry(owner,document_type='receipt',currency='USD',amount='123.45',document_date='2026-01-02')
        self.assertFalse(ledger.exclude_non_receipt(owner))
        db=ledger._db();self.assertEqual(db.execute('SELECT COUNT(*) FROM business_ledger_reviews WHERE entry_id=?',(identifier,)).fetchone()[0],1);db.close()

    def test_csv_supplied_snapshot_does_not_reread_new_rows(self):
        first=self.add()
        snapshot=ledger.list_entries(year=2026)
        self.add('Amount paid: USD 8.00',message_id='<new-after-snapshot@example.test>')
        with patch.object(ledger,'list_entries',side_effect=AssertionError('Snapshot export must not query again')):
            rows=list(csv.DictReader(io.StringIO(ledger.export_csv(year=2026,rows=snapshot))))
            self.assertEqual([row['entry_id'] for row in rows],[str(first)])
            self.assertEqual(list(csv.DictReader(io.StringIO(ledger.export_csv(rows=[])))),[])

    def test_accept_category_suggestion_atomically_preserves_latest_comment(self):
        identifier=self.add()
        ledger.set_category_suggestion(identifier,'Software','Service provider')
        ledger.update_metadata(identifier,comment='New note saved from another tab',category='')
        ledger.accept_category_suggestion(identifier,'Software')
        row=ledger.get_entry(identifier)
        self.assertEqual(row['category'],'Software');self.assertEqual(row['comment'],'New note saved from another tab')
        self.assertEqual(row['owner_confirmed'],0)
        with self.assertRaises(ValueError):ledger.accept_category_suggestion(identifier,'Software')
        other=self.add('Amount paid: USD 8.00',message_id='<other-category@example.test>')
        ledger.set_category_suggestion(other,'Hosting','Cloud provider')
        with self.assertRaises(ValueError):ledger.accept_category_suggestion(other,'Software')
        self.assertEqual(ledger.get_entry(other)['category'],'')

    def test_substantive_invoice_is_preserved_despite_scheduled_payment_prose(self):
        text='Invoice #ABC-123 Amount due: USD 50.00 Your card will be charged on October 1.'
        self.assertFalse(ledger.is_payment_reminder('Your invoice',text))
        result=ledger.extract(text,subject='Your invoice',source_date='2026-09-17')
        self.assertEqual(result['document_type'],'invoice');self.assertEqual(result['status'],'ready')
        self.assertTrue(ledger.is_payment_reminder('Upcoming payment','Your payment is coming up. Amount due: USD 50.00'))

    def test_ai_field_proposals_never_change_authoritative_values(self):
        import json
        identifier=self.add()
        ledger.confirm_entry(identifier,document_type='receipt',currency='USD',amount='123.45',document_date='2026-01-02',vendor='Owner vendor',category='Owner category',comment='')
        before=ledger.get_entry(identifier)
        fields=dict(vendor='Suggested vendor',document_date='2026-03-04',document_type='refund',reference=None,amount='9.99',currency='EUR',category='Suggested category',comment='Suggested note')
        self.assertFalse(ledger.set_ai_suggestions(identifier,fields,'Evidence summary',expected_source_digest='stale'))
        self.assertTrue(ledger.set_ai_suggestions(identifier,fields,'Evidence summary',expected_source_digest=before['source_digest']))
        after=ledger.get_entry(identifier)
        self.assertEqual(json.loads(after['ai_suggestions']),fields)
        for field in ('vendor','document_date','document_type','reference','amount_minor','currency','category','comment','owner_confirmed','metadata_confirmed','status'):
            self.assertEqual(after[field],before[field])
        self.assertEqual(ledger.summaries()[0]['net_paid_minor'],12345)
        # The setter serializes a snapshot; changing the caller's dictionary cannot
        # retroactively alter either stored proposals or their audited history.
        fields['vendor']='Mutated later'
        self.assertEqual(json.loads(ledger.get_entry(identifier)['ai_suggestions'])['vendor'],'Suggested vendor')
        db=ledger._db();audit=json.loads(db.execute('SELECT after_json FROM business_ledger_reviews ORDER BY id DESC LIMIT 1').fetchone()[0]);db.close()
        self.assertEqual(json.loads(audit['ai_suggestions'])['vendor'],'Suggested vendor')

    def test_ai_proposals_validate_every_field_and_unknowns_can_be_null(self):
        identifier=self.add()
        self.assertTrue(ledger.set_ai_suggestions(identifier,dict(vendor=None,document_date=None,document_type=None,reference=None,amount=None,currency=None,category=None,comment=None),'No usable evidence'))
        invalid=({'unknown':'field'},{'vendor':12},{'document_date':'2026-02-30'},{'document_type':'unknown'},{'reference':'x'*81},{'amount':'-2.00'},{'amount':'1e9'},{'amount':'1.234','currency':'USD'},{'currency':'XYZ'},{'category':'x'*121},{'comment':'x'*4001})
        for fields in invalid:
            with self.subTest(fields=repr(fields)[:80]),self.assertRaises(ValueError):ledger.set_ai_suggestions(identifier,fields,'Evidence')

    def test_full_confirmation_is_atomic_and_explicit_blank_metadata_is_preserved(self):
        identifier=self.add('Total: $12.00')
        ledger.confirm_entry(identifier,document_type='receipt',currency='CAD',amount='12.00',document_date='2026-02-03',vendor='Owner vendor',category='',comment='')
        row=ledger.get_entry(identifier)
        self.assertEqual((row['owner_confirmed'],row['metadata_confirmed']),(1,1))
        self.assertEqual((row['vendor'],row['currency'],row['category'],row['comment']),('Owner vendor','CAD','',''))
        db=ledger._db();self.assertEqual(db.execute('SELECT COUNT(*) FROM business_ledger_reviews WHERE entry_id=?',(identifier,)).fetchone()[0],1);db.close()
        with self.assertRaises(ValueError):ledger.confirm_entry(identifier,document_type='receipt',currency='USD',amount='99.00',document_date='2026-02-03',vendor='',comment='Must not save')
        self.assertEqual(ledger.get_entry(identifier)['currency'],'CAD')
        self.assertEqual(ledger.get_entry(identifier)['comment'],'')
        ledger.confirm_entry(identifier,document_type='receipt',currency='CAD',amount='13.00',document_date='2026-02-03')
        self.assertEqual(ledger.get_entry(identifier)['vendor'],'Owner vendor')
        other=self.add(message_id='<blank-metadata@example.test>')
        ledger.update_metadata(other,category='',comment='')
        self.assertEqual(ledger.get_entry(other)['metadata_confirmed'],1)
