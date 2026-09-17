import importlib
import json
from unittest.mock import patch
from test_web_security import AppTestCase
import decision_bulk as bulk
import decision_suggestions as suggestions


class DecisionBatchTests(AppTestCase):
    def setUp(self):
        super().setUp()
        db=bulk._db()
        suggestions._schema(db)
        with db:
            for table in ('decision_batches','decision_batch_items','decision_suggestion_jobs','decision_suggestion_items'):
                db.execute('DELETE FROM '+table)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='decision_choice_feedback'").fetchone(): db.execute('DELETE FROM decision_choice_feedback')
        db.close()
        self.patchers=[]
        for name in ('tahor_db','mailbox_settings','ai_routing'):
            p=patch.object(suggestions,name,importlib.import_module(name));p.start();self.addCleanup(p.stop)
        p=patch.object(suggestions.mailbox_settings,'is_ai_enabled',return_value=True);p.start();self.addCleanup(p.stop)
        p=patch.object(suggestions,'_preferences',side_effect=lambda conn:{'feedback':suggestions.owner_feedback(conn)});p.start();self.addCleanup(p.stop)
        p=patch.object(suggestions.ai_routing,'reconcile_pending');p.start();self.addCleanup(p.stop)
        p=patch.object(suggestions.ai_routing,'record_result');p.start();self.addCleanup(p.stop)

    def add(self,kind='message_review',resolution=None,status='pending',context=None):
        conn=self.module.tahor_db.get_db()
        with conn:
            identifier=conn.execute('INSERT INTO decisions(kind,summary,context,status,resolution,created_at) VALUES(?,?,?,?,?,?)',(kind,'Account notice',json.dumps(context or {'sender_email':'news@example.com','subject':'Please review your account','excerpt':'A useful notice.'}),status,json.dumps(resolution) if resolution else None,'2026-01-01')).lastrowid
        conn.close();return identifier

    def selection(self,identifier,action='keep'):
        conn=self.module.tahor_db.get_db();row=conn.execute('SELECT * FROM decisions WHERE id=?',(identifier,)).fetchone();conn.close()
        return dict(decision_id=identifier,action=action,source_revision=bulk.decision_revision(row))

    def model(self,context,**kwargs):
        return [dict(decision_id=item['decision_id'],action='keep' if item['kind']=='message_review' else 'unsorted',bucket='',vendor_name='',confidence=.9,reason='Review the useful notice.') for item in context['untrusted_candidates']]

    def test_atomic_stale_batch_and_idempotent_owner_authorization(self):
        first=self.add();second=self.add();a=self.selection(first);b=self.selection(second)
        conn=self.module.tahor_db.get_db()
        with conn:conn.execute("UPDATE decisions SET context='{}' WHERE id=?",(second,))
        with self.assertRaises(bulk.SelectionConflict) as conflict:bulk.enqueue([a,b],'batch-original')
        self.assertEqual(conflict.exception.unavailable_ids,[second])
        self.assertIsNone(conn.execute('SELECT resolution FROM decisions WHERE id=?',(first,)).fetchone()[0])
        job=bulk.enqueue([a],'batch-original')
        self.assertEqual(bulk.enqueue([a],'batch-original')['job_id'],job['job_id'])
        self.assertEqual(json.loads(conn.execute('SELECT resolution FROM decisions WHERE id=?',(first,)).fetchone()[0]),{'action':'keep'})
        with self.assertRaises(ValueError):bulk.enqueue([dict(a,action='trash')],'batch-original')
        conn.close()

    def test_rule_approval_and_path_escape_never_bulk_authorized(self):
        rule=self.add('free_text_rule')
        with self.assertRaises(bulk.SelectionConflict):bulk.enqueue([self.selection(rule,'approve')],'invalid-rule')
        vendor=self.add('vendor_mapping')
        choice=dict(self.selection(vendor,'map'),bucket='../unsafe',vendor_name='Shop')
        with self.assertRaises(ValueError):bulk.enqueue([choice],'invalid-map')
        choice.update(bucket='Shopping',vendor_name='Shop')
        self.assertEqual(bulk.enqueue([choice],'valid-map')['items'][0]['status'],'queued')

    def test_skip_leaves_review_pending_and_never_calls_executor(self):
        identifier=self.add();job=bulk.enqueue([self.selection(identifier,'skip')],'skip-request')
        with patch.object(self.module.apply_decisions,'apply_one') as execute:
            bulk.run_pending();execute.assert_not_called()
        self.assertEqual(job['status'],'complete')
        conn=self.module.tahor_db.get_db();row=conn.execute('SELECT * FROM decisions WHERE id=?',(identifier,)).fetchone();conn.close()
        self.assertTrue(bulk.eligible(row))

    def test_background_actions_use_existing_executor_and_sanitize_failure(self):
        identifier=self.add();job=bulk.enqueue([self.selection(identifier)],'apply-request')
        with patch.object(self.module.apply_decisions,'apply_one',side_effect=RuntimeError('PRIVATE BODY')):
            self.assertEqual(bulk.run_pending(),0)
        self.assertNotIn('PRIVATE',json.dumps(bulk.get_job(job['job_id'])))
        conn=bulk._db()
        with conn:conn.execute('UPDATE decision_batch_items SET retry_at=0')
        conn.close()
        with patch.object(self.module.apply_decisions,'apply_one',return_value='Done') as execute:
            self.assertEqual(bulk.run_pending(),1);execute.assert_called_once_with(identifier)
            self.assertEqual(bulk.run_pending(),0)

    def test_recommendations_async_no_actions_and_strict_stale_identity(self):
        identifier=self.add();self.add('free_text_rule');self.add(resolution={'action':'keep'})
        with patch.object(suggestions,'model_call',side_effect=self.model) as model,patch.object(self.module.apply_decisions,'apply_one') as execute:
            job=suggestions.enqueue(limit=20)
            self.assertEqual(job['total'],1);model.assert_not_called()
            self.assertEqual(suggestions.run_pending_jobs(),0)
            ready=suggestions.get_job(job['job_id'])
            self.assertEqual(ready['completed'],1);self.assertEqual(len(ready['recommendations']),1)
            self.assertEqual(ready['recommendations'][0]['source_revision'],self.selection(identifier)['source_revision'])
            execute.assert_not_called()
        conn=self.module.tahor_db.get_db()
        with conn:conn.execute("UPDATE decisions SET context='{}' WHERE id=?",(identifier,))
        conn.close()
        self.assertEqual(suggestions.get_job(job['job_id'])['recommendations'],[])
        self.assertEqual(suggestions.enqueue()['total'],1)

    def test_edit_during_model_request_discards_result(self):
        identifier=self.add();job=suggestions.enqueue()
        def changing(context,**kwargs):
            conn=self.module.tahor_db.get_db()
            with conn:conn.execute("UPDATE decisions SET context='{}' WHERE id=?",(identifier,))
            conn.close();return self.model(context)
        with patch.object(suggestions,'model_call',side_effect=changing):suggestions.run_pending_jobs()
        self.assertEqual(suggestions.get_job(job['job_id'])['recommendations'],[])

    def test_feedback_contains_queued_owner_intent_excludes_automatic_mapping(self):
        identifier=self.add();bulk.enqueue([self.selection(identifier,'keep_brief')],'owner-feedback')
        self.add('vendor_mapping',{'action':'map','bucket':'Shopping','vendor_name':'Shop','automatic_vendor_mapping':True},'resolved')
        conn=self.module.tahor_db.get_db();feedback=suggestions.owner_feedback(conn);conn.close()
        self.assertEqual(feedback['action_counts'],{'keep_brief':1})
        self.assertEqual(feedback['recent_explicit_choices'][0]['sender_email'],'news@example.com')

    def test_model_failure_durable_sanitized_retry_and_illegal_actions_rejected(self):
        identifier=self.add();job=suggestions.enqueue()
        with patch.object(suggestions,'model_call',side_effect=RuntimeError('PRIVATE BODY')):
            self.assertEqual(suggestions.run_pending_jobs(),1)
        self.assertNotIn('PRIVATE',json.dumps(suggestions.get_job(job['job_id'])))
        bad=dict(decision_id=identifier,action='approve',confidence=.9,reason='No',bucket='',vendor_name='')
        with self.assertRaises(ValueError):suggestions.validate({'recommendations':[bad]},[{'decision_id':identifier,'kind':'message_review'}])

    def test_model_transport_forces_private_route_and_validates_result(self):
        import io
        import os
        context={'untrusted_candidates':[{'decision_id':1,'kind':'message_review'}]}
        result=self.model(context)
        seen=[]
        def routed(task,registry,call,**kwargs):
            self.assertEqual(task,'decisions')
            return call('grok-4.6')
        def response(request,**kwargs):
            seen.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'recommendations':result})}}]}).encode())
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':'synthetic'}),patch.object(suggestions.ai_routing,'run',side_effect=routed),patch.object(suggestions.urllib.request,'urlopen',side_effect=response):
            self.assertEqual(suggestions.model_call(context,1,'test'),result)
        self.assertTrue(seen[0]['provider']['zdr'])
        self.assertEqual(seen[0]['provider']['data_collection'],'deny')
        self.assertEqual(seen[0]['provider']['only'],['xai/zdr'])

    def test_decision_settings_have_independent_policy_batch_size_and_guidance(self):
        settings=suggestions.mailbox_settings
        settings.set_ai_task_settings('decisions','free',paid_model='grok-4.6',free_model='ling-free',batch_size=23,guidance='Prefer keeping uncertain notices.')
        self.assertEqual(settings.get_ai_policy('decisions'),'free')
        self.assertEqual(settings.get_decision_batch_size(),23)
        self.assertEqual(settings.load_settings()['decision_guidance'],'Prefer keeping uncertain notices.')
        self.assertEqual(settings.get_subscription_batch_size(),50)

    def test_every_explicit_choice_retained_and_latest_sender_intent_wins(self):
        identifier=self.add()
        conn=self.module.tahor_db.get_db()
        row=conn.execute('SELECT * FROM decisions WHERE id=?',(identifier,)).fetchone()
        with conn:
            for action in ('keep','trash','skip','keep_brief'):
                bulk.record_choice_feedback(conn,row,{'action':action})
        feedback=suggestions.owner_feedback(conn)
        self.assertEqual(feedback['action_counts'],{'keep':1,'trash':1,'skip':1,'keep_brief':1})
        self.assertEqual(feedback['recent_explicit_choices'][0]['choice'],{'action':'keep_brief'})
        conn.close()
