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

    def test_year_filter_compact_rows_and_removal(self):
        body=self.client.get('/expenses?year=2026').get_data(as_text=True)
        self.assertIn('July 2026',body)
        self.assertIn('2026 total',body)
        self.assertIn('Year totals by category',body)
        self.assertIn('class="card expense-row"',body)
        self.assertNotIn('class="card expense-row" open',body)
        self.assertNotIn('&lt;Example &amp; Co&gt;',self.client.get('/expenses?year=2025').get_data(as_text=True))
        self.assertEqual(self.client.get('/expenses?year=bad').status_code,400)
        response=self.client.post('/expenses/'+str(self.identifier)+'/exclude',data={'csrf_token':self.token(),'year':'2026'})
        self.assertEqual(response.status_code,302)
        self.assertIn('year=2026',response.headers['Location'])
        body=self.client.get('/expenses?year=2026').get_data(as_text=True)
        self.assertNotIn('&lt;Example &amp; Co&gt;',body)
        self.assertNotIn('excluded',self.client.get('/expenses.csv?year=2026').get_data(as_text=True))

    def test_notes_category_and_ai_acceptance_do_not_confirm_amounts(self):
        route='/expenses/'+str(self.identifier)+'/metadata'
        self.assertEqual(self.client.post(route,data={'category':'Software'}).status_code,400)
        response=self.client.post(route,data={'csrf_token':self.token(),'year':'2026','category':'Software','comment':'<script>audit</script>'})
        self.assertEqual(response.status_code,302)
        row=business_ledger.get_entry(self.identifier)
        self.assertEqual(row['category'],'Software');self.assertEqual(row['owner_confirmed'],0)
        self.assertIn('&lt;script&gt;audit&lt;/script&gt;',self.client.get('/expenses?year=2026').get_data(as_text=True))
        business_ledger.update_metadata(self.identifier,category='',comment='keep latest comment')
        business_ledger.set_category_suggestion(self.identifier,'Cloud hosting','Recurring infrastructure')
        accept='/expenses/'+str(self.identifier)+'/accept-category'
        self.assertEqual(self.client.post(accept,data={'csrf_token':self.token(),'suggestion':'Wrong'}).status_code,409)
        self.assertEqual(self.client.post(accept,data={'csrf_token':self.token(),'suggestion':'Cloud hosting'}).status_code,302)
        self.assertEqual(business_ledger.get_entry(self.identifier)['comment'],'keep latest comment')

    def test_zip_is_private_and_complete_about_missing_originals(self):
        import io,json,zipfile
        self.assertEqual(self.module.app.test_client().get('/expenses.zip').status_code,302)
        response=self.client.get('/expenses.zip?year=2026')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertIn('expenses.csv',archive.namelist())
            self.assertIn('reviews.json',archive.namelist())
            manifest=json.loads(archive.read('manifest.json'))
            self.assertFalse(manifest['complete'])
            self.assertEqual(manifest['originals'][0]['entry_id'],self.identifier)
        empty=self.client.get('/expenses.zip?year=2025')
        with zipfile.ZipFile(io.BytesIO(empty.data)) as archive:
            self.assertEqual(json.loads(archive.read('ledger.json')),[])
        response.close();empty.close()

    def test_invalid_year_does_not_mutate_expense(self):
        self.assertEqual(self.client.post('/expenses/'+str(self.identifier)+'/exclude',data={'csrf_token':self.token(),'year':'bad'}).status_code,400)
        self.assertNotEqual(business_ledger.get_entry(self.identifier)['status'],'excluded')

    def test_ai_prefills_all_unconfirmed_fields_but_preserves_owner_edits(self):
        fields=dict(vendor='Example Software',document_date='2026-07-14',document_type='receipt',reference='AI-123',amount='25.00',currency='USD',category='Software subscriptions',comment='Developer tool usage')
        business_ledger.set_ai_suggestions(self.identifier,fields,'Receipt evidence reviewed')
        body=self.client.get('/expenses?year=2026').get_data(as_text=True)
        self.assertIn('value="Example Software"',body)
        self.assertIn('Developer tool usage',body)
        self.assertIn('value="AI-123"',body)
        self.assertEqual(business_ledger.get_entry(self.identifier)['vendor'],'<Example & Co>')
        response=self.client.post('/expenses/'+str(self.identifier)+'/confirm',data=dict(csrf_token=self.token(),year='2026',vendor='Reviewed vendor',document_date='2026-07-13',document_type='receipt',reference='OWNER',amount='24.00',currency='USD',category='',comment=''))
        self.assertEqual(response.status_code,302)
        body=self.client.get('/expenses?year=2026').get_data(as_text=True)
        self.assertIn('value="Reviewed vendor"',body)
        self.assertNotIn('value="Example Software"',body)
        self.assertNotIn('Developer tool usage',body)
        self.assertEqual(business_ledger.get_entry(self.identifier)['amount_minor'],2400)

    def test_accounting_downloads_exclude_removed_unpaid_unreviewed_and_duplicates(self):
        import io, json, zipfile, csv
        base=business_ledger.get_entry(self.identifier)
        for label, updates in [('excluded', {'status':'excluded'}), ('invoice', {'document_type':'invoice'}), ('pending', {'status':'review_needed'}), ('duplicate', {'duplicate_of':self.identifier})]:
            row=dict(base, id=100+len(label), **updates)
            with patch.object(business_ledger, 'list_entries', return_value=[base,row]):
                response=self.client.get('/expenses.zip?year=2026')
                with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                    self.assertEqual([x['id'] for x in json.loads(archive.read('ledger.json'))],[self.identifier])
                    self.assertEqual(len(list(csv.DictReader(io.StringIO(archive.read('expenses.csv').decode())))),1)
                response.close()
                text=self.client.get('/expenses.csv?year=2026').get_data(as_text=True)
                self.assertEqual(len(list(csv.DictReader(io.StringIO(text)))),1)
                response=self.client.get('/expenses.zip?year=2026&purpose=records')
                with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                    self.assertEqual(len(json.loads(archive.read('ledger.json'))),2)
                response.close()
        refund=dict(base,document_type='refund',amount_minor=-2500)
        self.assertEqual(business_ledger.accounting_entries([refund]),[refund])
        self.assertEqual(self.client.get('/expenses.zip?purpose=typo').status_code,400)
