import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import subscription_suggestions as suggestions


class SubscriptionSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start(patch.object(suggestions.tahor_db, 'DB_PATH', self.root / 'decisions.db'))
        self.start(patch.object(suggestions.mailbox_settings, 'SETTINGS_PATH', self.root / 'settings.json'))
        self.start(patch.object(suggestions.ai_routing, 'state_path', return_value=self.root / 'routing.json'))
        self.start(patch.object(suggestions.ai_routing, 'mailbox_settings', suggestions.mailbox_settings))
        self.start(patch.dict(os.environ, {'DATA_DIR':str(self.root), 'PROMPT_PATH':str(self.root/'prompt.txt'), 'OPENROUTER_API_KEY':'synthetic-key'}))
        self.start(patch('coupon_expiry.policies', return_value={'wanted.example': {'folder':'Coupons'}}))
        self.read_excerpts = suggestions._excerpts
        self.excerpts = self.start(patch.object(suggestions, '_excerpts', return_value={}))
        suggestions.tahor_db.init_db()
        self.now = self.start(patch.object(suggestions.time, 'time', return_value=1000))

    def start(self, patcher):
        value = patcher.start(); self.addCleanup(patcher.stop); return value

    def db(self):
        conn = suggestions.tahor_db.get_db()
        self.addCleanup(conn.close)
        return conn

    def add(self, count=1):
        return [suggestions.tahor_db.upsert_unsubscribe_candidate(f'shop{i}.example',f'news@shop{i}.example','Shop',f'https://shop{i}.example/remove?SECRET_TOKEN',None,True) for i in range(count)]

    def result(self, context, **kwargs):
        return [dict(candidate_id=item['candidate_id'],action='unsubscribe_block_marketing',confidence=.9,reason='Commercial updates; preserve receipts.') for item in context['untrusted_candidates']]

    def test_background_queue_is_bounded_durable_and_never_executes_actions(self):
        self.add(13)
        with patch.object(suggestions, 'model_call', side_effect=self.result) as model, patch.object(suggestions.tahor_db, 'execute_unsubscribe') as execute:
            job = suggestions.enqueue()
            model.assert_not_called()
            self.assertEqual(job['total'],13)
            self.assertEqual(suggestions.enqueue()['job_id'],job['job_id'])
            suggestions.run_pending_jobs()
            first = suggestions.get_job(job['job_id'])
            self.assertEqual((first['status'],first['completed']),('queued',10))
            suggestions.run_pending_jobs()
            final = suggestions.get_job(job['job_id'])
            self.assertEqual((final['status'],final['completed']),('complete',13))
            execute.assert_not_called()
            self.assertEqual(len(suggestions.latest_recommendations()),13)
            self.assertEqual(suggestions.enqueue()['total'],0)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM unsubscribe_candidates WHERE status='pending'").fetchone()[0],13)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sender_rules').fetchone()[0],0)

    def test_failure_is_sanitized_retries_and_expired_lease_recovers(self):
        self.add();job=suggestions.enqueue()
        with patch.object(suggestions, 'model_call', side_effect=OSError('PRIVATE_TOKEN')):
            self.assertEqual(suggestions.run_pending_jobs(),1)
        state=suggestions.get_job(job['job_id'])
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(state));self.assertEqual(state['status'],'queued')
        with patch.object(suggestions, 'model_call', side_effect=self.result) as model:
            suggestions.run_pending_jobs();model.assert_not_called()
            self.now.return_value=1301
            with self.db() as conn:
                conn.execute("UPDATE subscription_suggestion_jobs SET status='running',retry_at=1500,lease_token='crashed'")
            suggestions.run_pending_jobs();model.assert_not_called()
            self.now.return_value=1501
            suggestions.run_pending_jobs()
        self.assertEqual(suggestions.get_job(job['job_id'])['status'],'complete')

    def test_owner_choices_during_generation_are_not_overwritten_or_recommended(self):
        identifier=self.add()[0];job=suggestions.enqueue()
        def model(context,**kwargs):
            with self.db() as conn:
                conn.execute("UPDATE unsubscribe_candidates SET status='dismissed' WHERE id=?",(identifier,))
            return self.result(context)
        with patch.object(suggestions,'model_call',side_effect=model):suggestions.run_pending_jobs()
        self.assertEqual(suggestions.get_job(job['job_id'])['recommendations'],[])
        self.assertEqual(suggestions.latest_recommendations(),[])

    def test_lease_compare_and_swap_rejects_late_worker(self):
        self.add();job=suggestions.enqueue()
        def model(context,**kwargs):
            with self.db() as conn:
                conn.execute("UPDATE subscription_suggestion_jobs SET lease_token='new-owner'")
            return self.result(context)
        with patch.object(suggestions,'model_call',side_effect=model):suggestions.run_pending_jobs()
        self.assertEqual(suggestions.get_job(job['job_id'])['completed'],0)

    def test_context_uses_private_preferences_but_not_tracking_urls(self):
        self.add()
        self.root.joinpath('prompt.txt').write_text('Keep community updates.')
        suggestions.mailbox_settings.set_ai_task_settings('subscriptions','paid',guidance='Keep offers from shops I value.')
        with self.db() as conn:
            context=suggestions.build_context(conn,conn.execute('SELECT * FROM unsubscribe_candidates').fetchall())
        self.assertIn('Keep offers',context['trusted_owner_preferences']['subscription_guidance'])
        self.assertIn('wanted.example',context['trusted_owner_preferences']['coupon_senders'])
        self.assertNotIn('SECRET_TOKEN',json.dumps(context))

    def test_strict_schema_identity_and_blanket_block_guards(self):
        candidates=[dict(candidate_id=1,has_unsubscribe=True,non_compliant=0,explicit_block_all=False)]
        valid=dict(candidate_id=1,action='dismiss',confidence=.8,reason='Uncertain relationship.')
        for changes in ({'candidate_id':True},{'candidate_id':2},{'confidence':float('nan')},{'action':'delete'},{'reason':'bad\ntext'},{'action':'block_all','confidence':1}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                suggestions.validate({'recommendations':[dict(valid,**changes)]},candidates)
        with self.assertRaises(ValueError):suggestions.validate({'recommendations':[valid,valid]},candidates)
        candidates[0]['explicit_block_all']=True
        self.assertEqual(suggestions.validate({'recommendations':[dict(valid,action='block_all',confidence=.96)]},candidates)[0]['action'],'block_all')
        candidates[0]['has_unsubscribe']=False
        with self.assertRaises(ValueError):suggestions.validate({'recommendations':[dict(valid,action='unsubscribe')]},candidates)

    def test_each_policy_keeps_separate_models_and_private_provider_controls(self):
        self.add()
        with self.db() as conn:
            context=suggestions.build_context(conn,conn.execute('SELECT * FROM unsubscribe_candidates').fetchall())
        requests=[]
        def response(request,**kwargs):
            payload=json.loads(request.data);requests.append(payload)
            self.assertTrue(payload['provider']['zdr']);self.assertEqual(payload['provider']['data_collection'],'deny')
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'recommendations':self.result(context)})}}]}).encode())
        with patch.object(suggestions.urllib.request,'urlopen',side_effect=response):
            suggestions.mailbox_settings.set_ai_task_settings('subscriptions','free',paid_model='gpt5',free_model='ling-free')
            suggestions.model_call(context,1,'test')
        self.assertEqual(requests[0]['model'],'inclusionai/ling-3.0-flash-vl:free')
        self.assertEqual(requests[0]['provider']['only'],['novita'])
        self.assertEqual(requests[0]['provider']['max_price'],{'prompt':0,'completion':0})

    def test_policy_change_before_fallback_never_calls_newly_forbidden_tier(self):
        self.add()
        with self.db() as conn:
            context=suggestions.build_context(conn,conn.execute('SELECT * FROM unsubscribe_candidates').fetchall())
        def fail(request,**kwargs):
            suggestions.mailbox_settings.set_ai_policy('subscriptions','paid_only')
            raise OSError('temporary provider outage')
        with patch.object(suggestions.urllib.request,'urlopen',side_effect=fail) as request:
            with self.assertRaises(suggestions.ai_routing.RoutingUnavailable):suggestions.model_call(context,1,'test')
            self.assertEqual(request.call_count,1)

    def test_disabled_task_and_invalid_settings_fail_before_model_work(self):
        before=suggestions.mailbox_settings.load_settings()
        for value in (0,201,True,'abc','²'):
            with self.assertRaises(ValueError):suggestions.mailbox_settings.set_ai_task_settings('subscriptions','paid',batch_size=value)
            self.assertEqual(suggestions.mailbox_settings.load_settings(),before)
        suggestions.mailbox_settings.set_ai_task_settings('subscriptions','paid',paid_model='none')
        with self.assertRaises(ValueError):suggestions.enqueue()
        with patch.object(suggestions,'model_call') as model:
            suggestions.run_pending_jobs();model.assert_not_called()

    def test_changed_private_guidance_invalidates_old_recommendations(self):
        self.add();first=suggestions.enqueue()
        with patch.object(suggestions,'model_call',side_effect=self.result):suggestions.run_pending_jobs()
        self.assertEqual(len(suggestions.latest_recommendations()),1)
        suggestions.mailbox_settings.set_ai_task_settings('subscriptions','paid',guidance='Keep these subscriptions now.')
        self.assertEqual(suggestions.latest_recommendations(),[])
        self.assertEqual(suggestions.get_job(first['job_id'])['recommendations'],[])
        second=suggestions.enqueue()
        self.assertNotEqual(first['job_id'],second['job_id']);self.assertEqual(second['total'],1)

    def test_changed_guidance_during_generation_discards_old_result(self):
        self.add();job=suggestions.enqueue()
        def model(context,**kwargs):
            suggestions.mailbox_settings.set_ai_task_settings('subscriptions','paid',guidance='Keep community subscriptions.')
            return self.result(context)
        with patch.object(suggestions,'model_call',side_effect=model):suggestions.run_pending_jobs()
        self.assertEqual(suggestions.get_job(job['job_id'])['recommendations'],[])
        self.assertIn('Preferences changed',suggestions.get_job(job['job_id'])['error'])

    def test_batch_excludes_manual_selections_and_rejects_invalid_exclusions(self):
        identifiers=self.add(3)
        with self.assertRaises(ValueError):suggestions.enqueue(exclude_ids=[True])
        job=suggestions.enqueue(limit=2,exclude_ids=identifiers[:2])
        self.assertEqual(job['total'],1)
        with patch.object(suggestions,'model_call',side_effect=self.result):suggestions.run_pending_jobs()
        self.assertEqual([row['candidate_id'] for row in suggestions.latest_recommendations()],identifiers[2:])

    def test_shared_platform_never_gets_a_broad_block_recommendation(self):
        candidate=dict(candidate_id=1,has_unsubscribe=True,shared_delivery_domain=True,explicit_block_all=True)
        for action in ('block_all','unsubscribe','unsubscribe_block_marketing'):
            with self.assertRaises(ValueError):
                suggestions.validate({'recommendations':[dict(candidate_id=1,action=action,confidence=1,reason='A merchant advertised.')]},[candidate])

    def test_body_prefix_requires_exact_uid_validity_id_and_sender_and_uses_peek(self):
        import imaplib
        import fetch_batch
        from unittest.mock import Mock
        client=Mock();client.select.return_value=('OK',[])
        raw=b'From: News <news@example.com>\r\nMessage-ID: <real@example.com>\r\nContent-Type: text/plain\r\n\r\nCommunity update'
        client.uid.return_value=('OK',[(b'1 (UID 7)',raw)])
        sample=dict(mailbox='INBOX',uid='7',uidvalidity='42',message_id='<real@example.com>',sender_email='news@example.com')
        with patch.object(imaplib,'IMAP4_SSL',return_value=client),patch.object(fetch_batch,'mailbox_uidvalidity',return_value='42'),patch.dict(os.environ,{'FASTMAIL_EMAIL':'owner@example.com','FASTMAIL_APP_PASSWORD':'synthetic'}):
            self.assertIn('Community update',self.read_excerpts([(1,sample)])[1])
            self.assertEqual(client.select.call_args.kwargs,{'readonly':True})
            self.assertIn('BODY.PEEK[]<0.16384>',client.uid.call_args.args[2])
            client.uid.return_value=('OK',[(b'1 (UID 8)',raw)])
            self.assertEqual(self.read_excerpts([(1,sample)]),{})
            client.uid.return_value=('OK',[(b'1 (UID 7)',raw.replace(b'news@example.com',b'attacker@example.com'))])
            self.assertEqual(self.read_excerpts([(1,sample)]),{})
