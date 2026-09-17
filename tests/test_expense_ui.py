"""Expense review stays owner-only and never changes receipt messages."""
from unittest.mock import patch
from test_web_security import AppTestCase
import business_ledger


class ExpensePageTests(AppTestCase):
    def setUp(self):
        super().setUp()
        db=business_ledger._db()
        with db: db.execute('DELETE FROM business_ledger')
        db.close()
        self.identifier=business_ledger.record_receipt(dict(business_key='example-services',matched_rule_id='rule1',mailbox='INBOX',message_id='<receipt@example.test>',uid='1',uidvalidity='2',sender_email='billing@example.test',vendor='<Example & Co>',received_at='2026-07-15T12:00:00+00:00',subject='Payment receipt'),'Amount paid: USD 25.00\nReceipt ID: R-123',verified_business=True)

    def test_private_page_escape_and_csv(self):
        response=self.client.get('/expenses')
        self.assertEqual(response.status_code,200)
        self.assertIn('&lt;Example &amp; Co&gt;',response.get_data(as_text=True))
        self.assertIn('USD 25.00',response.get_data(as_text=True))
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        exported=self.client.get('/expenses.csv')
        self.assertEqual(exported.status_code,200)
        self.assertIn('attachment;',exported.headers['Content-Disposition'])
        anonymous=self.module.app.test_client()
        for route in ('/expenses','/expenses.csv','/expenses/message/'+str(self.identifier)):
            self.assertEqual(anonymous.get(route).status_code,302)

    def test_confirm_requires_csrf_and_only_changes_ledger(self):
        route='/expenses/'+str(self.identifier)+'/confirm'
        self.assertEqual(self.client.post(route,data={}).status_code,400)
        with patch('message_reviews.read_message') as mailbox_read:
            response=self.client.post(route,data={'csrf_token':self.token(),'document_type':'receipt','currency':'USD','amount':'28.75','document_date':'2026-07-14'})
            self.assertEqual(response.status_code,302)
            mailbox_read.assert_not_called()
        self.assertEqual(business_ledger.get_entry(self.identifier)['amount_minor'],2875)

    def test_receipt_preview_escapes_body_and_remains_in_modal(self):
        with patch('message_reviews.read_message',return_value=({'subject':'Receipt','sender':'billing@example.test'},'<script>untrusted</script>')):
            response=self.client.get('/expenses/message/'+str(self.identifier))
        self.assertEqual(response.status_code,200)
        self.assertIn('&lt;script&gt;untrusted&lt;/script&gt;',response.get_data(as_text=True))
        self.assertIn('expenses\\/message',response.get_data(as_text=True))
