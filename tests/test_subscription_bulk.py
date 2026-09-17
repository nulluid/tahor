import json
from unittest.mock import patch
from test_web_security import AppTestCase
import subscription_bulk


class SubscriptionBulkTests(AppTestCase):
    def setUp(self):
        super().setUp()
        db=subscription_bulk._db()
        with db:
            db.execute("DELETE FROM subscription_actions")
            db.execute("DELETE FROM subscription_batches")
        db.close()

    def candidate(self, domain='shop.example'):
        self.module.tahor_db.upsert_unsubscribe_candidate(domain,'news@'+domain,'Shop','https://'+domain+'/unsubscribe',None,True)
        return self.module.tahor_db.get_unsubscribe_candidate(domain)['id']

    def enqueue(self,candidate,action='unsubscribe',key='request-123'):
        return subscription_bulk.enqueue([{'candidate_id':candidate,'action':action}],key)

    def test_only_explicit_authenticated_submission_queues_work(self):
        candidate=self.candidate()
        page=self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertIn('data-apply',page);self.assertIn('type="radio"',page)
        self.assertIn('value="" checked',page)
        self.assertNotIn('class="subscription-form"',page)
        with patch.object(self.module.tahor_db,'execute_unsubscribe') as send:
            self.assertEqual(self.client.post('/unsubscribe/batches',data={}).status_code,400)
            response=self.client.post('/unsubscribe/batches',data={'csrf_token':self.token(),'request_key':'request-valid','selections':json.dumps([{'candidate_id':candidate,'action':'unsubscribe'}])})
            self.assertEqual(response.status_code,202)
            send.assert_not_called()
        self.assertNotEqual(self.module.app.test_client().get('/unsubscribe/batches').status_code,200)

    def test_duplicate_submit_and_worker_pass_never_send_twice(self):
        candidate=self.candidate();job=self.enqueue(candidate)
        self.assertEqual(self.enqueue(candidate)['job_id'],job['job_id'])
        with patch.object(self.module.tahor_db,'execute_unsubscribe',return_value='Requested') as send:
            subscription_bulk.run_pending();subscription_bulk.run_pending()
            send.assert_called_once()
        self.assertEqual(subscription_bulk.get_job(job['job_id'])['items'][0]['status'],'done')
        self.assertEqual(self.module.tahor_db.get_unsubscribe_candidate('shop.example')['status'],'unsubscribed')

    def test_ambiguous_delivery_after_crash_is_not_repeated(self):
        candidate=self.candidate();job=self.enqueue(candidate)
        db=subscription_bulk._db()
        with db:db.execute("UPDATE subscription_actions SET status='sending',started_at='2020-01-01T00:00:00+00:00'")
        db.close()
        with patch.object(self.module.tahor_db,'execute_unsubscribe') as send:
            subscription_bulk.run_pending();send.assert_not_called()
        self.assertEqual(subscription_bulk.get_job(job['job_id'])['items'][0]['status'],'uncertain')
        with self.assertRaises(ValueError):self.enqueue(candidate,key='second-request')

    def test_local_retry_uses_saved_delivery_result(self):
        candidate=self.candidate();job=self.enqueue(candidate)
        with patch.object(self.module.tahor_db,'execute_unsubscribe',return_value='Requested') as send:
            with patch.object(subscription_bulk,'_finish',side_effect=RuntimeError('disk busy')):subscription_bulk.run_pending()
            subscription_bulk.run_pending()
            send.assert_called_once()
        self.assertEqual(subscription_bulk.get_job(job['job_id'])['status'],'complete')

    def test_sender_change_and_invalid_mixed_batch_send_nothing(self):
        first=self.candidate();second=self.candidate('other.example')
        with self.assertRaises(ValueError):subscription_bulk.enqueue([{'candidate_id':first,'action':'unsubscribe'},{'candidate_id':second,'action':'trash'}],'invalid-batch')
        self.assertEqual(subscription_bulk.recent_jobs(),[])
        job=self.enqueue(first)
        db=self.module.tahor_db.get_db()
        with db:db.execute("UPDATE unsubscribe_candidates SET unsubscribe_url='https://changed.example/' WHERE id=?",(first,))
        db.close()
        with patch.object(self.module.tahor_db,'execute_unsubscribe') as send:
            subscription_bulk.run_pending();send.assert_not_called()
        self.assertEqual(subscription_bulk.get_job(job['job_id'])['items'][0]['status'],'attention')

    def test_block_marketing_survives_remote_failure_and_keep_sends_nothing(self):
        first=self.candidate();second=self.candidate('other.example')
        job=subscription_bulk.enqueue([{'candidate_id':first,'action':'unsubscribe_block_marketing'},{'candidate_id':second,'action':'dismiss'}],'batch-block-keep')
        with patch.object(self.module.tahor_db,'execute_unsubscribe',side_effect=RuntimeError('network error')) as send,patch.object(self.module.generate_sieve,'refresh_sieve'):
            subscription_bulk.run_pending();send.assert_called_once()
        self.assertEqual(self.module.tahor_db.get_sender_rule('shop.example'),'block_marketing')
        self.assertEqual([item['status'] for item in subscription_bulk.get_job(job['job_id'])['items']],['attention','done'])

    def test_legacy_individual_action_cannot_repeat_a_queued_batch(self):
        candidate=self.candidate();self.enqueue(candidate)
        with patch.object(self.module.tahor_db,'execute_unsubscribe') as send:
            response=self.client.post(f'/unsubscribe/{candidate}',data={'csrf_token':self.token(),'action':'unsubscribe'})
            self.assertEqual(response.status_code,409)
            send.assert_not_called()

    def test_idempotency_key_cannot_be_reused_for_changed_intent(self):
        candidate=self.candidate();self.enqueue(candidate)
        with self.assertRaises(ValueError):self.enqueue(candidate,action='block_all')
