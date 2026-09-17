import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

import business_filing as business


class BusinessRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.file=self.root/'business_filing.json'
        self.patch=patch.object(business,'rules_path',return_value=self.file);self.patch.start();self.addCleanup(self.patch.stop)
        self.config={'version':1,'businesses':[{'id':'example','root':'Business/Example','rules':[{'id':'service','vendor':'Service','domains':['service.example'],'since':'2026-07-01'}]}]}
        self.save()
        search=patch.object(business,'search_uids',side_effect=lambda conn,*criteria: conn.uid('SEARCH',None,*criteria))
        search.start();self.addCleanup(search.stop)

    def save(self):self.file.write_text(json.dumps(self.config))
    def record(self,**changes):return dict({'id':'<receipt@example.com>','from':'billing@service.example','subject':'Your receipt for service','date':'2026-07-01T00:10:00-07:00'},**changes)

    def test_private_rules_date_boundary_and_calendar_receipt_folder(self):
        route=business.match_message(self.record(),{'category':'receipt'})
        self.assertEqual(route['destination'],'Business/Example/Receipts/2026')
        self.assertEqual(route['business_key'],'example')
        self.assertIsNone(business.match_message(self.record(date='2026-06-30T23:59:59-07:00'),{'category':'receipt'}))
        self.assertIsNone(business.match_message(self.record(**{'from':'billing@service.example.attacker.test'}),{'category':'receipt'}))

    def test_receipt_protection_overrides_trash_but_preserves_attention(self):
        result={'action':'trash','category':'receipt','retention':'transient','needs_attention':True}
        business.protect_classification(result,self.record())
        self.assertEqual((result['action'],result['retention'],result['expense_type']),('keep','forever','business'))
        self.assertTrue(result['needs_attention']);self.assertIn('business-receipt',result['business_keywords'])

    def test_marketing_is_not_promoted_to_a_receipt_or_expense(self):
        result={'action':'trash','category':'marketing','retention':'transient'}
        route=business.protect_classification(result,self.record(subject='New products and discounts'))
        self.assertFalse(route['is_receipt']);self.assertEqual(result['action'],'trash');self.assertNotIn('expense_type',result)

    def test_invoice_status_is_not_assumed_paid(self):
        route=business.match_message(self.record(subject='Invoice #123 is due'),{'category':'receipt'})
        self.assertEqual(route['document_type'],'invoice')
        self.assertEqual(business.match_message(self.record(subject='Refund confirmation'),{'category':'receipt'})['document_type'],'refund')

    def test_specific_purchase_is_not_every_message_from_merchant(self):
        rule=self.config['businesses'][0]['rules'][0];rule.pop('domains');rule.pop('since');rule['message_ids']=['<purchase@example.com>'];self.save()
        self.assertIsNone(business.match_message(self.record(),{'category':'receipt'}))
        self.assertIsNotNone(business.match_message(self.record(id='<purchase@example.com>'),{'category':'receipt'}))

    def test_conflicting_business_ownership_never_guesses(self):
        self.config['businesses'].append(dict(id='other',root='Business/Other',rules=self.config['businesses'][0]['rules']));self.save()
        with self.assertRaises(ValueError):business.match_message(self.record(),{'category':'receipt'})

    def test_invalid_config_rejected_and_no_config_is_noop(self):
        self.config['businesses'][0]['root']='Trash';self.save()
        with self.assertRaises(ValueError):business.load_rules()
        self.file.unlink();self.assertEqual(business.load_rules(),[])
        client=Mock();self.assertTrue(business.run_sweep(client)['complete']);client.uid.assert_not_called()

    def client(self,flags='category-receipt',mailbox='INBOX'):
        client=Mock();client.capabilities=(b'MOVE',);client.select.return_value=('OK',[]);client.response.return_value=('UIDVALIDITY',[b'42'])
        client.list.return_value=('OK',[b'(\\HasNoChildren) "/" "'+mailbox.encode()+b'"'])
        raw=b'From: billing@service.example\r\nMessage-ID: <receipt@example.com>\r\nSubject: Your receipt\r\nDate: Wed, 01 Jul 2026 12:00:00 +0000\r\n\r\nPayment received USD 10.00'
        def command(action,*args):
            if action=='SEARCH':return 'OK',[b'7']
            if action=='FETCH':return 'OK',[(b'1 (UID 7 FLAGS ('+flags.encode()+b') INTERNALDATE "01-Jul-2026 12:00:00 +0000")',raw)]
            if action in ('STORE','MOVE'):return 'OK',[]
            raise AssertionError(action)
        client.uid.side_effect=command
        return client

    def test_backfill_protects_before_move_and_never_marks_read_or_deletes(self):
        client=self.client()
        with patch('business_ledger.record_receipt') as ledger,patch('filing_sweep.ensure_folder'),patch.object(business.tahor_db,'relocate_vendor_samples'):
            result=business.run_sweep(client,backfill=True,state_path=self.root/'state.json')
        self.assertEqual(result['moved'],1);ledger.assert_called_once()
        operations=[call.args for call in client.uid.call_args_list]
        store=next(i for i,args in enumerate(operations) if args[0]=='STORE');move=next(i for i,args in enumerate(operations) if args[0]=='MOVE')
        self.assertLess(store,move);self.assertIn('retention-forever',operations[store][-1])
        self.assertNotIn('\\Seen',str(operations));self.assertNotIn('EXPUNGE',str(operations));self.assertNotIn('DELETE',str(operations))

    def test_attention_deferral_is_durable_and_receipt_still_protected(self):
        client=self.client(flags='category-receipt needs-attention')
        with patch('business_ledger.record_receipt'),patch('filing_sweep.ensure_folder'):
            result=business.run_sweep(client,backfill=True,state_path=self.root/'state.json')
        self.assertEqual((result['moved'],result['deferred']),(0,1))
        self.assertEqual(json.loads((self.root/'state.json').read_text())['folders']['INBOX']['deferred'],[7])
        self.assertTrue(any(call.args[0]=='STORE' for call in client.uid.call_args_list))
        self.assertFalse(any(call.args[0]=='MOVE' for call in client.uid.call_args_list))

    def test_changed_exact_identity_is_not_moved(self):
        client=self.client();original=client.uid.side_effect;fetches=[0]
        def command(action,*args):
            result=original(action,*args)
            if action=='FETCH':
                fetches[0]+=1
                if fetches[0]==2:return 'OK',[(result[1][0][0],result[1][0][1].replace(b'<receipt@example.com>',b'<other@example.com>'))]
            return result
        client.uid.side_effect=command
        with self.assertRaises(RuntimeError):business.run_sweep(client,backfill=True,state_path=self.root/'state.json')
        self.assertFalse(any(call.args[0] in ('STORE','MOVE') for call in client.uid.call_args_list))

    def test_cursor_resumes_in_large_folder_without_reprocessing_all_messages(self):
        client=self.client(mailbox='Archive')
        with patch('business_ledger.record_receipt'),patch('filing_sweep.ensure_folder'),patch.object(business.tahor_db,'relocate_vendor_samples'):
            business.run_sweep(client,backfill=True,state_path=self.root/'state.json',limit=1)
            client.uid.reset_mock()
            business.run_sweep(client,backfill=True,state_path=self.root/'state.json',limit=1)
        self.assertFalse(any(call.args[0]=='FETCH' for call in client.uid.call_args_list))


    def test_dry_run_never_mutates_mail_ledger_or_cursor(self):
        client=self.client()
        path=self.root/'state.json'
        with patch('business_ledger.record_receipt') as ledger:
            result=business.run_sweep(client,backfill=True,dry_run=True,state_path=path)
        self.assertEqual(result['moved'],1)
        self.assertFalse(path.exists());ledger.assert_not_called()
        self.assertFalse(any(c.args[0] in ('STORE','MOVE') for c in client.uid.call_args_list))

    def test_generation_change_prevents_any_mutation(self):
        client=self.client()
        with patch.object(business.fetch_batch,'mailbox_uidvalidity',side_effect=['42','43']):
            with self.assertRaisesRegex(RuntimeError,'generation changed'):
                business.run_sweep(client,backfill=True,state_path=self.root/'state.json')
        self.assertFalse(any(c.args[0] in ('STORE','MOVE') for c in client.uid.call_args_list))

    def test_copyuid_refreshes_ledger_location_without_fabricating_identity(self):
        client=self.client()
        client.response.side_effect=lambda name: (name,[b'98 7 101']) if name=='COPYUID' else (name,[b'42'])
        with patch('business_ledger.record_receipt') as ledger,patch('filing_sweep.ensure_folder'),patch.object(business.tahor_db,'relocate_vendor_samples'):
            business.run_sweep(client,backfill=True,state_path=self.root/'state.json')
        self.assertEqual(ledger.call_count,2)
        saved=ledger.call_args.args[0]
        self.assertEqual((saved['mailbox'],saved['uidvalidity'],saved['uid']),('Business/Example/Receipts/2026','98','101'))

    def test_failed_move_retries_and_removes_old_deletion_markers_after_protection(self):
        client=self.client(flags='category-receipt retention-transient delete-pending')
        original=client.uid.side_effect
        client.uid.side_effect=lambda action,*args: ('NO',[]) if action=='MOVE' else original(action,*args)
        path=self.root/'state.json'
        with patch('business_ledger.record_receipt'),patch('filing_sweep.ensure_folder'):
            with self.assertRaises(RuntimeError):business.run_sweep(client,backfill=True,state_path=path)
        stores=[c.args for c in client.uid.call_args_list if c.args[0]=='STORE']
        self.assertIn('retention-forever',stores[0][-1]);self.assertIn('delete-pending',stores[1][-1])
        self.assertFalse(path.exists())
        client.uid.side_effect=original
        with patch('business_ledger.record_receipt'),patch('filing_sweep.ensure_folder'),patch.object(business.tahor_db,'relocate_vendor_samples'):
            self.assertEqual(business.run_sweep(client,backfill=True,state_path=path)['moved'],1)
