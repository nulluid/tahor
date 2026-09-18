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

    def test_upcoming_payment_is_correspondence_even_when_mistagged_receipt(self):
        route = business.match_message(self.record(subject='Your upcoming payment reminder', body='Your payment of USD 45.00 is scheduled for next week.'), {'category':'receipt'})
        self.assertFalse(route['is_receipt'])
        self.assertEqual(route['destination'], 'Business/Example/Correspondence/Service')

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


    def test_inventory_finishes_across_multiple_bounded_folder_runs(self):
        client=self.client();client.list.return_value=('OK',[b'(\\HasNoChildren) "/" "Archive A"',b'(\\HasNoChildren) "/" "Archive B"'])
        path=self.root/'state.json'
        with patch('business_ledger.record_receipt'),patch('filing_sweep.ensure_folder'),patch.object(business.tahor_db,'relocate_vendor_samples'):
            first=business.run_sweep(client,backfill=True,state_path=path,limit=1)
            second=business.run_sweep(client,backfill=True,state_path=path,limit=1)
        self.assertFalse(first['complete']);self.assertTrue(second['complete'])
        self.assertEqual(json.loads(path.read_text())['visited'],[])


class CandidateSearchGrammarTests(unittest.TestCase):
    def query(self, rules):
        client=Mock()
        with patch.object(business,'search_uids',return_value=('OK',[b'11 13'])) as search:
            self.assertEqual(business._candidate_search(client,rules,10),[b'11',b'13'])
        return ' '.join(search.call_args.args[1:])

    def matches(self, query, sender='', subject='', identifier='', business_flag=False):
        # Independent small IMAP SEARCH grammar parser: OR consumes exactly two
        # search keys; parenthesized key lists are conjunctions, not OR lists.
        import re
        tokens=re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+',query)
        position=0
        def take():
            nonlocal position
            token=tokens[position];position+=1
            return json.loads(token) if token.startswith('"') else token
        def key():
            operation=take()
            if operation=='(':
                values=[]
                while tokens[position]!=')':values.append(key())
                take();return all(values)
            if operation=='OR':
                left=key();right=key();return left or right
            if operation=='ALL':return True
            if operation=='FROM':return take().casefold() in sender.casefold()
            if operation=='SUBJECT':return take().casefold() in subject.casefold()
            if operation=='KEYWORD':
                self.assertEqual(take(),'expense-business');return business_flag
            if operation=='HEADER':
                self.assertEqual(take(),'Message-ID');return take() in identifier
            if operation=='UID':
                self.assertEqual(take(),'11:*');return True
            raise AssertionError('Unexpected IMAP search key '+operation)
        values=[]
        while position<len(tokens):values.append(key())
        return all(values)

    def test_each_rule_keeps_its_own_source_and_subject_union(self):
        query=self.query([
            dict(domains=['large.example'],subject_contains_any=['Cloud','Developer']),
            dict(senders=['personal@large.example']),
            dict(domains=['small.example']),
            dict(message_ids=['<specific@example>']),
            dict(classified_business=True),
        ])
        for sender,subject,identifier,flag,expected in [
            ('offers@large.example','Shopping','',False,False),
            ('billing@large.example','Cloud invoice','',False,True),
            ('account@large.example','Developer account','',False,True),
            ('personal@large.example','Unrelated','',False,True),
            ('sales@other.example','Cloud invoice','',False,False),
            ('news@small.example','Unrelated','',False,True),
            ('sales@other.example','Unrelated','<specific@example>',False,True),
            ('sales@other.example','Unrelated','',True,True),
        ]:
            with self.subTest(sender=sender,subject=subject):
                self.assertEqual(self.matches(query,sender,subject,identifier,flag),expected)

    def test_multiple_source_types_are_alternatives_before_subject_conjunction(self):
        query=self.query([dict(senders=['one@example.org'],domains=['two.example'],message_ids=['<three@example>'],classified_business=True,subject_contains_any=['Receipt'])])
        for sender,identifier,flag in [('one@example.org','',False),('any@two.example','',False),('other@example.net','<three@example>',False),('other@example.net','',True)]:
            self.assertTrue(self.matches(query,sender,'Receipt',identifier,flag))
            self.assertFalse(self.matches(query,sender,'Sales offer',identifier,flag))

    def test_non_ascii_alternative_keeps_broad_source_and_dates_remain_local(self):
        query=self.query([dict(domains=['example.org'],subject_contains_any=['Cloud','Développeur'],body_contains_any=['private text'],since='2026-07-01')])
        self.assertTrue(self.matches(query,'a@example.org','Anything'))
        self.assertNotIn('SUBJECT',query);self.assertNotIn('SINCE',query);self.assertNotIn('BODY',query)
        self.assertNotIn('2026',query);self.assertNotIn('private text',query)

    def test_quoted_subject_escaping_is_valid_search_grammar(self):
        value='Plan "Pro" \\ annual'
        query=self.query([dict(domains=['example.org'],subject_contains_any=[value])])
        self.assertTrue(self.matches(query,'a@example.org','Your '+value+' receipt'))
        self.assertFalse(self.matches(query,'a@example.org','Other receipt'))

    def test_non_ascii_source_falls_back_without_dropping_its_rule(self):
        query=self.query([dict(message_ids=['<réçu@example.org>'],subject_contains_any=['Receipt'])])
        self.assertTrue(self.matches(query,'any@example.net','Receipt'))
        self.assertFalse(self.matches(query,'any@example.net','Unrelated'))


class HeaderPrefilterTests(unittest.TestCase):
    def rule(self, **changes):
        return dict(dict(id='service',vendor='Service',business_key='example',root='Business/Example',domains=['service.example'],since='2026-07-01'),**changes)

    def row(self, uid, sender='billing@service.example', subject='Your receipt', date='01 Jul 2026 12:00:00 +0000', extra=b'', internal='01-Sep-2026 12:00:00 +0000'):
        header=('1 (UID '+str(uid)+' FLAGS (category-receipt) INTERNALDATE "'+internal+'")').encode()
        raw=('From: '+sender+'\r\nSubject: '+subject+'\r\nDate: '+date+'\r\nMessage-ID: <'+str(uid)+'@example>\r\n').encode()+extra+b'\r\n'
        return header,raw

    def test_one_batch_rejects_one_hundred_proven_source_nonmatches(self):
        client=Mock();client.uid.return_value=('OK',[self.row(uid,sender='store@service.example',subject='Shopping sale') for uid in range(1,101)])
        uids=[str(uid).encode() for uid in range(1,101)]
        result=business._header_nonmatches(client,'Trash','42',uids,[self.rule(subject_contains_any=['Cloud'])])
        self.assertEqual(result,set(uids));client.uid.assert_called_once()
        self.assertIn('BODY.PEEK[HEADER.FIELDS',client.uid.call_args.args[-1])
        self.assertEqual(client.uid.call_args.args[1],b','.join(uids))

    def test_body_filters_are_removed_for_preliminary_match(self):
        client=Mock();client.uid.return_value=('OK',[self.row(1),self.row(2,sender='unrelated@example.org')])
        result=business._header_nonmatches(client,'Archive','42',[b'1',b'2'],[self.rule(body_contains_any=['Special purchase'])])
        self.assertEqual(result,{b'2'})

    def test_header_date_not_internaldate_controls_cutoff(self):
        client=Mock();client.uid.return_value=('OK',[self.row(1,date='01 Jul 2025 12:00:00 +0000'),self.row(2,internal='01-Sep-2025 12:00:00 +0000')])
        self.assertEqual(business._header_nonmatches(client,'Archive','42',[b'1',b'2'],[self.rule()]),{b'1'})

    def test_ambiguous_missing_and_truncated_evidence_always_gets_full_read(self):
        client=Mock()
        duplicate=self.row(2,sender='unrelated@example.org')
        truncated=self.row(3,sender='unrelated@example.org')
        client.uid.return_value=('OK',[
            self.row(1,sender='unrelated@example.org',extra=b'From: billing@service.example\r\n'),
            duplicate,duplicate,
            (truncated[0],truncated[1]+b'x'*16384),
            (b'1 (UID 4 FLAGS ())',b'bad header\r\n\r\n'),
        ])
        self.assertEqual(business._header_nonmatches(client,'Archive','42',[b'1',b'2',b'3',b'4',b'5'],[self.rule()]),set())
        client.uid.return_value=('NO',[])
        self.assertEqual(business._header_nonmatches(client,'Archive','42',[b'1'],[self.rule()]),set())

    def test_sweep_skips_full_body_and_mutations_for_proven_old_message(self):
        client=Mock();client.select.return_value=('OK',[]);client.response.return_value=('UIDVALIDITY',[b'42'])
        client.list.return_value=('OK',[b'(\\HasNoChildren) "/" "Trash"'])
        client.uid.return_value=('OK',[self.row(7,date='01 Jul 2025 12:00:00 +0000')])
        with tempfile.TemporaryDirectory() as directory,patch.object(business,'load_rules',return_value=[self.rule()]),patch.object(business,'_candidate_search',return_value=[b'7']),patch('business_ledger.record_receipt') as ledger:
            path=Path(directory)/'state.json'
            result=business.run_sweep(client,backfill=True,state_path=path)
            self.assertEqual(json.loads(path.read_text())['folders']['Trash']['after'],7)
        self.assertTrue(result['complete']);self.assertEqual(result['examined'],1);self.assertEqual(result['receipts'],0)
        client.uid.assert_called_once();ledger.assert_not_called()
        self.assertIn('HEADER.FIELDS',client.uid.call_args.args[-1])
