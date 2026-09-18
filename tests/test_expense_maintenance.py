import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import business_ledger as ledger
import expense_archive
import expense_maintenance
import fetch_batch


class ExpenseMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(importlib.import_module('tahor_db'),'DB_PATH',Path(self.temp.name)/'ledger.db');p.start();self.addCleanup(p.stop)

    def add(self, subject, text, number):
        raw=(f'From: billing@example.test\r\nMessage-ID: <{number}@example.test>\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{text}').encode()
        metadata=dict(business_key='example',matched_rule_id='example',vendor='Example',sender_email='billing@example.test',mailbox='INBOX',uid=str(number),uidvalidity='1',message_id=f'<{number}@example.test>',received_at='2026-09-17T00:00:00+00:00',subject=subject)
        identifier=ledger.record_receipt(metadata,fetch_batch.extract_body_text(raw),verified_business=True)
        expense_archive.store_verified(identifier,raw)
        return identifier

    def test_archived_refresh_recovers_amount_and_removes_unconfirmed_reminder(self):
        paid=self.add('Your receipt','Amount paid $21.75 Date paid Sep 17, 2026 Summary Amount paid : $21.75',1)
        reminder=self.add('Upcoming payment reminder','Your payment of USD 30.00 is scheduled for tomorrow.',2)
        db=ledger._db()
        with db:db.execute("UPDATE business_ledger SET amount_candidate='',review_reasons='[\"amount_missing\"]' WHERE id=?",(paid,))
        db.close()
        result=expense_maintenance.refresh_archived(limit=10)
        self.assertEqual(result,{'refreshed':2,'non_receipts_removed':1})
        self.assertEqual(ledger.get_entry(paid)['amount_candidate'],'21.75')
        self.assertEqual(ledger.get_entry(reminder)['status'],'excluded')
        self.assertEqual(expense_maintenance.refresh_archived(),{'refreshed':0,'non_receipts_removed':0})

    def test_confirmed_owner_values_survive_background_refresh(self):
        identifier=self.add('Your receipt','Amount paid: USD 20.00',1)
        ledger.confirm_entry(identifier,document_type='receipt',currency='USD',amount='19.00',document_date='2026-09-17')
        expense_maintenance.refresh_archived()
        self.assertEqual(ledger.get_entry(identifier)['amount_minor'],1900)
