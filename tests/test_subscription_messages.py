import json
from unittest.mock import Mock, patch
from test_web_security import AppTestCase
import subscription_messages


class SubscriptionViewerTests(AppTestCase):
    def setUp(self):
        super().setUp()
        for name, value in [('email_address', 'owner@example.test'), ('app_password', 'test-password')]:
            mocked = patch.object(subscription_messages.config, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def candidate(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('shop.example','news@shop.example','Example shop',None,None,False)
        db=self.module.tahor_db.get_db()
        identifier=db.execute('SELECT id FROM unsubscribe_candidates').fetchone()[0]
        db.close()
        return identifier

    def test_links_available_without_samples_and_only_owner_can_view(self):
        candidate=self.candidate()
        page=self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertIn(f'href="/subscription-messages/{candidate}"',page)
        with patch.object(subscription_messages,'scan',return_value={'complete':False}) as scan:
            response=self.client.get(f'/subscription-messages/{candidate}')
            self.assertEqual(response.status_code,200)
            scan.assert_called_once()
            self.assertIn('Find more emails',response.get_data(as_text=True))
        self.assertNotEqual(self.module.app.test_client().get(f'/subscription-messages/{candidate}').status_code,200)
        self.assertEqual(self.client.post(f'/subscription-messages/{candidate}').status_code,400)

    def test_cached_list_and_escaped_plaintext_message(self):
        candidate=self.candidate()
        identifier=self.module.tahor_db.record_subscription_sample(candidate,dict(mailbox='INBOX',message_id='<sample@example.com>',uid='7',uidvalidity='42',sender_email='news@shop.example',subject='<script>subject</script>',received_at='2026-09-01T12:00:00+00:00'))
        with patch.object(subscription_messages,'scan') as scan:
            page=self.client.get(f'/subscription-messages/{candidate}').get_data(as_text=True)
            scan.assert_not_called()
            self.assertIn('&lt;script&gt;subject',page)
            self.assertIn('days ago',page)
        import message_reviews
        with patch.object(message_reviews,'read_message',return_value=({'subject':'Example','sender':'news@shop.example'},'<img src="https://attacker.example/track">')) as read:
            response=self.client.get(f'/subscription-message/{candidate}/{identifier}')
            self.assertEqual(response.status_code,200)
            self.assertIn('&lt;img',response.get_data(as_text=True))
            self.assertNotIn('<img',response.get_data(as_text=True))
            self.assertEqual(read.call_args.args[0]['uidvalidity'],'42')
        self.assertEqual(self.client.get(f'/subscription-message/{candidate+100}/{identifier}').status_code,404)

    def test_legacy_scan_checks_exact_sender_and_uses_readonly_peek(self):
        candidate=self.candidate();db=self.module.tahor_db.get_db()
        client=Mock();client.select.return_value=('OK',[]);client.response.return_value=('UIDVALIDITY',[b'42'])
        wrong=b'From: news@shop.example.attacker.test\r\nMessage-ID: <wrong@example.com>\r\nSubject: wrong\r\n\r\n'
        good=b'From: Example <news@shop.example>\r\nMessage-ID: <right@example.com>\r\nSubject: Offer\r\n\r\n'
        client.uid.side_effect=[('OK',[b'7 8']),('OK',[(b'1 (UID 8 INTERNALDATE "01-Sep-2026 12:00:00 +0000")',wrong)]),('OK',[(b'1 (UID 7 INTERNALDATE "01-Sep-2026 12:00:00 +0000")',good)])]
        with patch.object(subscription_messages.imaplib,'IMAP4_SSL',return_value=client),patch.object(subscription_messages,'list_mailboxes',return_value=[('INBOX',[])]):
            state=subscription_messages.scan(db,candidate)
        self.assertTrue(state['complete'])
        samples=self.module.tahor_db.get_subscription_samples(candidate)
        self.assertEqual(len(samples),1);self.assertEqual(samples[0]['message_id'],'<right@example.com>')
        client.select.assert_called_once_with('"INBOX"',readonly=True)
        for call in client.uid.call_args_list:
            self.assertIn(call.args[0],('SEARCH','FETCH'))
            if call.args[0]=='FETCH':self.assertIn('BODY.PEEK',call.args[-1]);self.assertIn('<0.65537>',call.args[-1])
        db.close()

    def test_legacy_search_resumes_bounded_folders_without_mail_mutation(self):
        candidate=self.candidate();db=self.module.tahor_db.get_db()
        client=Mock();client.select.return_value=('OK',[]);client.response.return_value=('UIDVALIDITY',[b'42']);client.uid.return_value=('OK',[b''])
        with patch.object(subscription_messages.imaplib,'IMAP4_SSL',return_value=client),patch.object(subscription_messages,'list_mailboxes',return_value=[('INBOX',[]),('Archive',[]),('Receipts',[])]):
            self.assertFalse(subscription_messages.scan(db,candidate,folder_limit=1)['complete'])
            self.assertFalse(subscription_messages.scan(db,candidate,folder_limit=1)['complete'])
            self.assertTrue(subscription_messages.scan(db,candidate,folder_limit=1)['complete'])
        self.assertEqual([call.args[0] for call in client.select.call_args_list],['"INBOX"','"Archive"','"Receipts"'])
        self.assertTrue(all(call.args[0]=='SEARCH' for call in client.uid.call_args_list))
        db.close()
