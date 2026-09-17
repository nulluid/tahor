"""Review metadata reads and owner actions bind to exact mailbox identities."""
import json
import unittest
from unittest.mock import Mock, patch

import message_reviews
from test_web_security import AppTestCase


class MessageLookupTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.select.return_value = ('OK', [])
        self.client.list.return_value = ('OK', [])
        self.client.response.return_value = ('UIDVALIDITY', [b'42'])
        self.context = {'mailbox': 'INBOX', 'message_id': '<review@example.com>'}
        self.header = b'Message-ID: <review@example.com>\r\nSubject: Receipt\r\nFrom: Shop <shop@example.com>\r\nDate: Tue, 01 Sep 2026 12:30:00 +0000\r\n\r\n'
        self.metadata = b'1 (UID 7 INTERNALDATE "01-Sep-2026 12:30:00 +0000")'
        self.connect = patch.object(message_reviews.fetch_batch, 'connect', return_value=self.client)
        self.connect.start()
        self.addCleanup(self.connect.stop)

    def test_legacy_lookup_discards_substring_hits_and_returns_exact_metadata(self):
        self.client.uid.side_effect = [('OK', [b'6 7']), ('OK', [(b'1 (UID 6 INTERNALDATE "01-Sep-2026 12:30:00 +0000")', self.header.replace(b'<review@example.com>', b'<prefix-review@example.com>'))]), ('OK', [(self.metadata, self.header)])]
        result = message_reviews.locate(self.context)
        self.assertEqual(result, {'mailbox': 'INBOX', 'uid': '7', 'uidvalidity': '42', 'subject': 'Receipt', 'sender': 'Shop <shop@example.com>', 'date': 'Tue, 01 Sep 2026 12:30:00 +0000', 'received_at': '2026-09-01T12:30:00+00:00'})
        self.client.select.assert_called_once_with('"INBOX"', readonly=True)
        self.assertTrue(all('BODY.PEEK' in call.args[-1] for call in self.client.uid.call_args_list if call.args[0] == 'FETCH'))
        self.client.logout.assert_called_once()

    def test_stale_uidvalidity_uses_exact_message_search_instead_of_reused_uid(self):
        self.context.update(uid='1', uidvalidity='old')
        self.client.uid.side_effect = [('OK', [b'7']), ('OK', [(self.metadata, self.header)])]
        self.assertEqual(message_reviews.locate(self.context)['uid'], '7')
        self.assertEqual(self.client.uid.call_args_list[0].args[0], 'SEARCH')

    def test_deleted_original_never_substitutes_another_copy_in_same_folder(self):
        self.context.update(uid='6', uidvalidity='42')
        self.client.uid.side_effect = [('OK', [None]), ('OK', [b'7']), ('OK', [(self.metadata, self.header)])]
        with self.assertRaisesRegex(RuntimeError, 'different copy'):
            message_reviews.locate(self.context)
        self.client.logout.assert_called_once()

    def test_missing_or_duplicate_matches_never_pick_arbitrary_message(self):
        for replies in [[('OK', [b''])], [('OK', [b'7 8']), ('OK', [(self.metadata, self.header)]), ('OK', [(self.metadata.replace(b'UID 7', b'UID 8'), self.header)])]]:
            with self.subTest(replies=len(replies)):
                self.client.uid.side_effect = replies
                with self.assertRaises(RuntimeError):
                    message_reviews.locate(self.context)

    def test_moved_message_search_resumes_after_budget_and_revalidates_final_hit(self):
        self.client.list.return_value = ('OK', [b'(\\HasNoChildren) "/" "Archive"', b'(\\HasNoChildren) "/" "Receipts"'])
        self.client.uid.side_effect = [('OK', [b'']), ('OK', [b'7']), ('OK', [(self.metadata, self.header)])]
        with patch.object(message_reviews.time, 'monotonic', side_effect=[0, 0, 21]):
            with self.assertRaises(message_reviews.LookupPending):
                message_reviews.locate(self.context)
        self.assertEqual(self.context['review_search']['next_index'], 1)
        # A fresh process can resume from JSON, without rereading the first folder.
        restored = json.loads(json.dumps(self.context))
        self.client.uid.side_effect = [('OK', [b'']), ('OK', [b'']), ('OK', [(self.metadata, self.header)])]
        with patch.object(message_reviews.time, 'monotonic', return_value=30):
            result = message_reviews.locate(restored)
        self.assertEqual(result['mailbox'], 'Archive')
        self.assertNotIn('review_search', restored)

    def test_viewer_reads_without_seen_and_verifies_exact_identity_again(self):
        details = {'mailbox': 'Archive', 'uid': '7', 'uidvalidity': '42', 'subject': 'Receipt', 'sender': 'shop@example.com'}
        raw = self.header + b'Thank you for your order.'
        self.client.uid.return_value = ('OK', [(self.metadata, raw)])
        with patch.object(message_reviews, 'locate', return_value=details):
            _, body = message_reviews.read_message(self.context)
        self.assertIn('Thank you for your order.', body)
        self.assertIn('BODY.PEEK[]<0.', self.client.uid.call_args.args[-1])
        self.client.select.assert_called_once_with('"Archive"', readonly=True)
        self.client.uid.return_value = ('OK', [(self.metadata, raw.replace(b'<review@example.com>', b'<other@example.com>'))])
        with patch.object(message_reviews, 'locate', return_value=details), self.assertRaises(RuntimeError):
            message_reviews.read_message(self.context)

    def test_saved_uid_still_requires_exact_message_id(self):
        self.context.update(uid='7', uidvalidity='42')
        self.client.uid.side_effect = [('OK', [(self.metadata, self.header.replace(b'<review@example.com>', b'<different@example.com>'))]), ('OK', [b''])]
        with self.assertRaises(RuntimeError):
            message_reviews.locate(self.context)


class ReviewActionTests(AppTestCase):
    def post(self, url, **values):
        values['csrf_token'] = self.token()
        return self.client.post(url, data=values)

    def decision(self, context=None, summary=''):
        db = self.module.tahor_db.get_db()
        with db:
            row = db.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES('message_review',?,?,'pending','2026-09-01')", (summary, json.dumps(context or {'mailbox': 'INBOX', 'message_id': '<review@example.com>'})))
        identifier = row.lastrowid
        db.close()
        return identifier

    def row(self, identifier):
        db = self.module.tahor_db.get_db()
        row = db.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
        db.close()
        return row

    def test_keep_resolves_after_verified_apply_without_unrelated_git_dependency(self):
        identifier = self.decision()
        details = {'uid': '7', 'uidvalidity': '42', 'subject': 'Receipt', 'sender': 'Shop <shop@example.com>'}
        with patch.object(message_reviews, 'locate', return_value=details), patch('keyword_tool.apply_ops', return_value={'applied': {'<review@example.com>'}}) as apply, patch.object(self.module.apply_decisions, 'commit_and_push_data', side_effect=OSError('unrelated git failure')) as git:
            self.assertEqual(self.post(f'/resolve/{identifier}', action='keep').status_code, 302)
        self.assertEqual(self.row(identifier)['status'], 'resolved')
        self.assertTrue(json.loads(self.row(identifier)['context'])['applied'])
        self.assertEqual(apply.call_args.args[0][0]['uid'], '7')
        git.assert_not_called()
        self.assertNotIn(f'action="/resolve/{identifier}"', self.client.get('/').get_data(as_text=True))
        self.assertEqual(self.post(f'/resolve/{identifier}', action='trash').status_code, 409)

    def test_failed_operation_stays_pending_and_never_claims_applied(self):
        identifier = self.decision()
        with patch.object(message_reviews, 'locate', return_value={'uid': '7', 'uidvalidity': '42'}), patch('keyword_tool.apply_ops', return_value={'applied': set(), 'missing': {'<review@example.com>'}}):
            self.post(f'/resolve/{identifier}', action='keep')
        self.assertEqual(self.row(identifier)['status'], 'pending')
        self.assertFalse(json.loads(self.row(identifier)['context']).get('applied'))

    def test_changed_review_metadata_cannot_requeue_owner_decision(self):
        database = self.module.tahor_db
        database.queue_message_review('INBOX', '<review@example.com>', '', '7', '42')
        db = database.get_db()
        original = db.execute("SELECT * FROM decisions WHERE kind='message_review'").fetchone()
        context = dict(json.loads(original['context']), applied=True, outcome='Message kept')
        with db:
            db.execute("UPDATE decisions SET context=?,status='resolved' WHERE id=?", (json.dumps(context), original['id']))
        database.queue_message_review('INBOX', '<review@example.com>', 'Now known', '7', '42', metadata={'sender': 'Sender <sender@example.com>'})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions WHERE kind='message_review'").fetchone()[0], 1)
        db.close()

    def test_blank_card_has_safe_fallback_and_refresh_adds_sender_date_age(self):
        identifier = self.decision()
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('(No subject)', page)
        self.assertIn('Refresh message details', page)
        self.assertNotIn('&lt;review@example.com&gt;', page)
        details = {'uid': '7', 'uidvalidity': '42', 'subject': '<script>unsafe</script>', 'sender': '<b>Shop</b> shop@example.com', 'received_at': '2026-09-01T12:30:00+00:00'}
        with patch.object(message_reviews, 'locate', return_value=details):
            self.assertEqual(self.post(f'/message-details/{identifier}').status_code, 302)
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('2026-09-01 12:30 UTC', page)
        self.assertIn('days old', page)
        self.assertIn('&lt;b&gt;Shop&lt;/b&gt;', page)
        self.assertNotIn('<script>unsafe</script>', page)
        self.assertEqual(self.row(identifier)['status'], 'pending')

    def test_vendor_cards_show_exact_sender_samples_and_existing_folder_choices(self):
        db_module = self.module.tahor_db
        db_module.queue_vendor_mapping('t.shopifyemail.com', metadata={'sender_email': 'merchant@t.shopifyemail.com', 'display_name': 'Merchant Name', 'subject': 'Order confirmation', 'date': '2026-09-01', 'suggested_vendor': 'Merchant Name'})
        import config
        with patch.object(config, 'vendor_buckets', return_value={'existing.example': ['Financial/CreditCardStatements', 'Bank']}):
            page = self.client.get('/').get_data(as_text=True)
        self.assertIn('merchant@t.shopifyemail.com', page)
        self.assertIn('Order confirmation', page)
        self.assertIn('2026-09-01', page)
        self.assertIn('value="Financial/CreditCardStatements"', page)
        self.assertIn('value="Merchant Name"', page)
        db_module.queue_vendor_mapping('t.shopifyemail.com', metadata={'sender_email': 'different@t.shopifyemail.com', 'display_name': 'Other Merchant', 'subject': 'Receipt'})
        db = db_module.get_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions WHERE kind='vendor_mapping'").fetchone()[0], 2)
        db.close()

    def test_owner_choice_survives_search_budget_for_automatic_retry(self):
        identifier = self.decision()
        def progress(context):
            context['review_search'] = {'next_index': 2}
            raise message_reviews.LookupPending('Search will retry')
        with patch.object(message_reviews, 'locate', side_effect=progress):
            self.post(f'/resolve/{identifier}', action='keep')
        row = self.row(identifier)
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(json.loads(row['resolution'])['action'], 'keep')
        self.assertEqual(json.loads(row['context'])['review_search']['next_index'], 2)
        self.assertIn('will retry automatically', self.client.get('/').get_data(as_text=True))
        with patch.object(self.module.apply_decisions, 'apply_one') as apply, patch('ai_routing.reconcile_pending'):
            self.module.apply_decisions.main()
        apply.assert_called_once_with(identifier)
        self.post(f'/resolve/{identifier}', action='skip')
        self.assertIsNone(self.row(identifier)['resolution'])

    def test_email_view_requires_owner_and_escapes_message_markup(self):
        identifier = self.decision()
        anonymous = self.module.app.test_client()
        self.assertEqual(anonymous.get(f'/message/{identifier}').status_code, 302)
        with patch.object(message_reviews, 'read_message', return_value=({'subject': '<script>title</script>', 'sender': 'sender@example.com', 'received_at': '2026-09-01'}, '<img src="https://tracker.example/image">')):
            response = self.client.get(f'/message/{identifier}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        page = response.get_data(as_text=True)
        self.assertIn('&lt;img', page)
        self.assertNotIn('<img src=', page)
        self.assertNotIn('<script>title', page)

    def test_vendor_sample_action_is_message_scoped_and_moves_keep_links_current(self):
        module = self.module.tahor_db
        module.queue_vendor_mapping('shop.example', metadata={'sender_email': 'orders@shop.example', 'subject': 'Order receipt', 'mailbox': 'INBOX', 'message_id': '<review@example.com>', 'uid': '7', 'uidvalidity': '42'})
        db = module.get_db()
        vendor = db.execute("SELECT * FROM decisions WHERE kind='vendor_mapping'").fetchone()
        module.relocate_vendor_samples('INBOX', 'Filed/Shopping/Shop', ['<review@example.com>'])
        sample = json.loads(self.row(vendor['id'])['context'])['samples'][0]
        self.assertEqual(sample['mailbox'], 'Filed/Shopping/Shop')
        self.assertNotIn('uid', sample)
        with patch.object(message_reviews, 'locate', return_value={'mailbox': 'Filed/Shopping/Shop', 'uid': '9', 'uidvalidity': '50'}), patch('keyword_tool.apply_ops', return_value={'applied': {'<review@example.com>'}}) as apply:
            self.assertEqual(self.post(f'/vendor-message/{vendor["id"]}/0', action='trash').status_code, 302)
        self.assertEqual(apply.call_args.args[0][0]['mailbox'], 'Filed/Shopping/Shop')
        self.assertTrue(apply.call_args.args[0][0]['delete'])
        self.assertEqual(self.row(vendor['id'])['status'], 'resolved')
        self.assertIsNone(module.get_sender_rule('shop.example'))
        db.close()

    def test_selected_folder_overrides_ai_suggestion_without_hidden_custom_value(self):
        module = self.module.tahor_db
        module.queue_vendor_mapping('shop.example', metadata={'sender_email': 'orders@shop.example', 'suggested_bucket': 'Shopping/Retail', 'suggested_vendor': 'Shop'})
        db = module.get_db()
        vendor = db.execute("SELECT id FROM decisions WHERE kind='vendor_mapping'").fetchone()
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('value="Shopping/Retail" selected', page)
        self.assertNotIn('name="bucket_custom" value=', page)
        with patch.object(self.module.apply_decisions, 'apply_one', return_value='Saved'):
            self.post(f'/resolve/{vendor["id"]}', action='map', bucket='Financial/Receipts', bucket_custom='', vendor_name='Shop')
        resolution = json.loads(self.row(vendor['id'])['resolution'])
        self.assertEqual(resolution['bucket'], 'Financial/Receipts')
        db.close()

    def test_retry_claim_prevents_replacing_action_while_mailbox_operation_runs(self):
        identifier = self.decision()
        db = self.module.tahor_db.get_db()
        with db:
            db.execute('UPDATE decisions SET resolution=? WHERE id=?', (json.dumps({'action': 'keep'}), identifier))
        db.close()
        def locating(context):
            self.assertEqual(self.post(f'/resolve/{identifier}', action='trash').status_code, 409)
            return {'uid': '7', 'uidvalidity': '42'}
        with patch.object(message_reviews, 'locate', side_effect=locating), patch('keyword_tool.apply_ops', return_value={'applied': {'<review@example.com>'}}) as apply:
            self.module.apply_decisions.apply_one(identifier)
        self.assertFalse(apply.call_args.args[0][0]['delete'])
        self.assertEqual(json.loads(self.row(identifier)['resolution'])['action'], 'keep')

    def test_automatic_vendor_work_does_not_appear_as_manual_questions(self):
        module = self.module.tahor_db
        module.queue_vendor_mapping('shop.example', metadata={'sender_email': 'orders@shop.example', 'subject': 'Receipt'})
        with patch.object(self.module.mailbox_settings, 'is_ai_enabled', return_value=True):
            page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Automatic filing is processing 1 sender(s)', page)
        self.assertNotIn('Save routing rule', page)
        with patch.object(self.module.mailbox_settings, 'is_ai_enabled', return_value=False):
            page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Save routing rule', page)

    def test_keep_brief_and_skip_mean_different_things(self):
        identifier = self.decision()
        self.post(f'/resolve/{identifier}', action='skip')
        self.assertEqual(self.row(identifier)['status'], 'pending')
        with patch.object(message_reviews, 'locate', return_value={'uid': '7', 'uidvalidity': '42'}), patch('keyword_tool.apply_ops', return_value={'applied': {'<review@example.com>'}}) as apply:
            self.post(f'/resolve/{identifier}', action='keep_brief')
        operation = apply.call_args.args[0][0]
        self.assertIn('retention-standard', operation['add'])
        self.assertIn('retention-short-lived', operation['add'])
        self.assertTrue({'retention-forever', 'retention-coupon'} <= set(operation['remove']))
        self.assertNotIn('reply-protected', operation['remove'])
        from message_expiry import expired
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        flags = {b'retention-forever', b'retention-coupon'}
        flags.difference_update(flag.encode() for flag in operation['remove'])
        flags.update(flag.encode() for flag in operation['add'])
        self.assertTrue(expired(flags, now - timedelta(days=8), now, {'read': 3, 'unread': 7}))
        self.assertIn('retention-pending-review', operation['remove'])
        self.assertFalse(operation['delete'])
        self.assertEqual(self.row(identifier)['status'], 'resolved')
