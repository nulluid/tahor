"""Business selection and tax annotations remain scoped across every web action."""
import io
import json
import zipfile
from test_web_security import AppTestCase
import accounting_dashboard as accounting
import business_ledger as ledger


class AccountingRoutes(AppTestCase):
    def setUp(self):
        super().setUp()
        db=accounting._db()
        with db:
            for table in ('business_ledger','accounting_profiles','accounting_details','accounting_expected','accounting_audit'):
                db.execute('DELETE FROM '+table)
        db.close()
        accounting.save_profile('north',name='North Example',default=True)
        accounting.save_profile('south',name='South Example')
        self.ids={}
        for business in ('north','south'):
            self.ids[business]=ledger.record_receipt(dict(business_key=business,matched_rule_id='rule',mailbox='INBOX',message_id='<'+business+'@example.test>',uid='1',uidvalidity='2',sender_email='billing@example.test',vendor=business+' vendor',received_at='2026-07-15T00:00:00+00:00',subject='Receipt'), 'Amount paid: USD 100.00',verified_business=True)

    def test_default_business_scopes_pages_downloads_and_mutations(self):
        body=self.client.get('/expenses?year=2026').get_data(as_text=True)
        self.assertIn('north vendor',body);self.assertNotIn('south vendor',body)
        csv=self.client.get('/expenses.csv?year=2026').get_data(as_text=True)
        self.assertIn('north vendor',csv);self.assertNotIn('south vendor',csv)
        body=self.client.get('/expenses?year=2026&business=south').get_data(as_text=True)
        self.assertIn('south vendor',body);self.assertNotIn('north vendor',body)
        self.assertIn('business=south',body)
        self.assertEqual(self.client.post('/expenses/'+str(self.ids['south'])+'/exclude',data={'csrf_token':self.token(),'business':'north'}).status_code,404)
        self.assertEqual(self.client.get('/expenses?business=unknown').status_code,400)

    def test_accounting_edits_exact_allocations_and_export(self):
        identifier=self.ids['north']
        route='/expenses/'+str(identifier)+'/accounting'
        data=dict(csrf_token=self.token(),business='north',year='2026',tax_treatment='Proposed operating',tax_description='Cloud service',tax_form='Part V',notes='Evidence reviewed',business_use_percent='100',transaction_role='expense',allocation_category=['Development','Production'],allocation_percent=['80','20'],allocation_tax_treatment=['Proposed development','Operating'],allocation_tax_form=['Part V','Part V'],allocation_tax_description=['Development cloud','Live service'])
        self.assertEqual(self.client.post(route,data=data).status_code,302)
        details=accounting.entry_details(identifier)
        self.assertEqual([r['bps'] for r in details['allocations']],[8000,2000])
        data['allocation_percent']=['80','30']
        self.assertEqual(self.client.post(route,data=data).status_code,400)
        self.assertEqual([r['bps'] for r in accounting.entry_details(identifier)['allocations']],[8000,2000])
        response=self.client.get('/expenses.zip?year=2026&business=north')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertIn('allocation-lines.csv',archive.namelist())
            text=archive.read('allocation-lines.csv').decode()
            self.assertIn('80.00',text);self.assertIn('20.00',text);self.assertNotIn('south vendor',text)
            self.assertIn('assets.csv',archive.namelist())
        response.close()
        accounting.save_entry_details(identifier,transaction_role='financing_principal')
        self.assertNotIn('north vendor',self.client.get('/expenses.csv?year=2026').get_data(as_text=True))

    def test_expected_costs_do_not_enter_cash_and_reassignment_is_scoped(self):
        response=self.client.post('/expenses/expected',data=dict(csrf_token=self.token(),business='north',year='2026',vendor='Future Tool',amount='999.00',currency='USD',purpose='Planned subscription',status='expected'))
        self.assertEqual(response.status_code,302)
        self.assertEqual(len(accounting.list_expected('north',2026)),1)
        self.assertEqual(accounting.dashboard('north',2026)['cash_totals'][0]['amount_minor'],10000)
        self.assertNotIn('Future Tool',self.client.get('/expenses.csv?year=2026').get_data(as_text=True))
        response=self.client.post('/expenses/'+str(self.ids['north'])+'/reassign',data=dict(csrf_token=self.token(),business='north',target_business='south',year='2026'))
        self.assertEqual(response.status_code,302)
        self.assertEqual(ledger.get_entry(self.ids['north'])['business_key'],'south')
