import json
from unittest.mock import patch
from test_web_security import AppTestCase


class WebActionTests(AppTestCase):
    def test_inbox_timing_saves_both_values_and_rejects_invalid_updates(self):
        settings = self.module.mailbox_settings
        original = settings.load_settings()
        self.addCleanup(settings.save_settings, original)
        self.assertEqual(self.post('/settings', inbox_grace='1', inbox_read_days='3', inbox_unread_days='7').status_code, 302)
        self.assertEqual(settings.get_inbox_grace_days(), {'read': 3, 'unread': 7})
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('Changes save automatically.', page)
        for invalid in ('-1', '1.5', 'abc', '3651', ''):
            self.assertEqual(self.post('/settings', inbox_grace='1', inbox_read_days='12', inbox_unread_days=invalid).status_code, 400)
            self.assertEqual(settings.get_inbox_grace_days(), {'read': 3, 'unread': 7})
        settings.record_free_batch(10, 5)
        self.assertEqual(settings.get_inbox_grace_days(), {'read': 3, 'unread': 7})

    def test_reply_backup_is_visible_and_only_accepts_free_models(self):
        settings = self.module.mailbox_settings
        original = settings.load_settings()
        self.addCleanup(settings.save_settings, original)
        self.assertEqual(self.post('/settings', reply_backup_model='ling-free').status_code, 302)
        self.assertEqual(settings.get_reply_backup_model(), 'ling-free')
        self.assertEqual(self.post('/settings', reply_backup_model='gpt5').status_code, 400)
        self.assertEqual(settings.get_reply_backup_model(), 'ling-free')
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('Reply writing and verification', page)
        self.assertIn('name="free_model"', page)
        self.assertIn('Always free', page)
        self.assertEqual(self.post('/settings', reply_backup_model='none').status_code, 302)
        self.assertEqual(settings.get_reply_backup_model(), 'none')

    def test_successful_unsubscribe_records_request_time(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', 'https://example.com/unsubscribe', None, True)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe', return_value='Unsubscribe requested'):
            self.post(f'/unsubscribe/{row["id"]}', action='unsubscribe')
        result = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        self.assertEqual(result['status'], 'unsubscribed')
        self.assertIsNotNone(result['unsubscribed_at'])

    def test_marketing_block_preserves_provider_receipts(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', 'https://example.com/unsubscribe', None, True)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe', return_value='Unsubscribe requested'):
            self.post(f'/unsubscribe/{row["id"]}', action='unsubscribe_block_marketing')
        self.assertEqual(self.module.tahor_db.get_sender_rule('example.com'), 'block_marketing')
        self.assertNotIn('discard;', (self.root / 'sieve.txt').read_text())

    def test_confirming_sieve_proposal_resolves_its_banner(self):
        id = self.decision('sieve_update')
        self.post(f'/dismiss-sieve/{id}')
        db = self.module.tahor_db.get_db()
        self.assertEqual(db.execute('SELECT status FROM decisions WHERE id=?', (id,)).fetchone()['status'], 'resolved')
        db.close()

    def test_leaving_vendor_unsorted_does_not_recreate_the_same_question(self):
        self.module.tahor_db.queue_vendor_mapping('example.com')
        db = self.module.tahor_db.get_db()
        id = db.execute("SELECT id FROM decisions WHERE kind='vendor_mapping'").fetchone()['id']
        db.close()
        self.post(f'/resolve/{id}', action='skip')
        self.module.tahor_db.queue_vendor_mapping('example.com')
        db = self.module.tahor_db.get_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions WHERE kind='vendor_mapping'").fetchone()[0], 1)
        db.close()

    def test_failed_sieve_refresh_can_be_retried_from_the_app(self):
        with patch.object(self.module.generate_sieve, 'refresh_sieve', side_effect=RuntimeError('temporary failure')):
            self.assertEqual(self.post('/refresh-sieve').status_code, 302)
        self.assertIn('Could not refresh', self.client.get('/').get_data(as_text=True))
        self.post('/refresh-sieve')
        self.assertTrue((self.root / 'sieve.txt').is_file())

    def test_invalid_settings_and_triggers_are_reported(self):
        for data in ({'classify_mode': 'unknown'}, {'rule_model': 'unknown'}, {'reply_model': 'unknown'}, {}):
            self.assertEqual(self.post('/settings', **data).status_code, 400)
        self.assertEqual(self.post('/add-reply-trigger', trigger_type='unknown', value='person@example.com').status_code, 400)

    def test_code_change_rule_is_not_reported_as_applied(self):
        with patch.object(self.module.apply_decisions, 'rule_model_call', return_value={'kind': 'needs_code_change', 'explanation': 'A new integration is required.'}):
            self.post('/add-rule', rule_text='Add a new integration')
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('has not been applied', page)
        self.assertIn('Retry rule', page)
        db = self.module.tahor_db.get_db()
        self.assertEqual(db.execute("SELECT status FROM decisions WHERE kind='free_text_rule'").fetchone()['status'], 'pending')
        db.close()

    def post(self, path, **data):
        return self.client.post(path, data=dict(data, csrf_token=self.token()))

    def decision(self, kind, context=None, resolution=None):
        db = self.module.tahor_db.get_db()
        with db:
            cur = db.execute('INSERT INTO decisions(kind,summary,context,resolution,created_at) VALUES (?, ?, ?, ?, ?)', (kind, 'Test', json.dumps(context or {}), json.dumps(resolution) if resolution else None, 'now'))
        db.close()
        return cur.lastrowid

    def test_vendor_mapping_is_applied_immediately(self):
        id = self.decision('vendor_mapping', {'sender_label': 'shop'})
        response = self.post(f'/resolve/{id}', action='map', bucket='Shopping', vendor_name='Example store')
        self.assertEqual(response.status_code, 302)
        content = json.loads((self.root / 'vendor_buckets.json').read_text())
        self.assertEqual(content['shop'], ['Shopping', 'Example store'])
        self.assertIn('mapped shop', self.client.get('/').get_data(as_text=True))

    def test_bad_mapping_remains_pending(self):
        id = self.decision('vendor_mapping')
        self.assertEqual(self.post(f'/resolve/{id}', action='map', bucket='', vendor_name='').status_code, 400)
        db = self.module.tahor_db.get_db()
        self.assertEqual(db.execute('SELECT status FROM decisions WHERE id=?', (id,)).fetchone()['status'], 'pending')
        db.close()

    def test_rule_failure_can_be_retried_without_resubmitting(self):
        with patch.object(self.module.apply_decisions, 'rule_model_call', side_effect=RuntimeError('provider unavailable')):
            self.assertEqual(self.post('/add-rule', rule_text='Keep my receipts').status_code, 302)
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Retry rule', page)
        db = self.module.tahor_db.get_db()
        id = db.execute("SELECT id FROM decisions WHERE kind='free_text_rule'").fetchone()['id']
        db.close()
        with patch.object(self.module.apply_decisions, 'rule_model_call', return_value={'kind': 'file_edit', 'prompt_txt': 'Keep receipts.', 'explanation': 'Keep receipts'}):
            self.assertEqual(self.post(f'/retry-rule/{id}').status_code, 302)
        self.assertIn('Approve these changes', self.client.get('/').get_data(as_text=True))
        db = self.module.tahor_db.get_db()
        token = json.loads(db.execute('SELECT context FROM decisions WHERE id=?', (id,)).fetchone()['context'])['rule_proposal']['token']
        db.close()
        with patch.object(self.module.apply_decisions, 'rule_model_call') as model:
            self.post(f'/review-rule/{id}', action='approve', proposal=token)
            model.assert_not_called()
        self.assertEqual((self.root / 'prompt.txt').read_text(), 'Keep receipts.')
        self.assertNotIn('Retry rule', self.client.get('/').get_data(as_text=True))

    def test_unsubscribe_failure_stays_pending_and_is_visible(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', 'https://example.com/unsubscribe', None, True)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe', side_effect=RuntimeError('unreachable')):
            self.post(f'/unsubscribe/{row["id"]}', action='unsubscribe')
        self.assertEqual(self.module.tahor_db.get_unsubscribe_candidate('example.com')['status'], 'pending')
        self.assertIn('The unsubscribe request could not be confirmed', self.client.get('/unsubscribe').get_data(as_text=True))

    def test_block_and_sieve_survive_unsubscribe_failure(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', 'https://example.com/unsubscribe', None, True)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe', side_effect=RuntimeError('unreachable')):
            self.post(f'/unsubscribe/{row["id"]}', action='block_all')
        self.assertEqual(self.module.tahor_db.get_sender_rule('example.com'), 'block_all')
        self.assertIn('"example.com"', self.module.generate_sieve.DATA_DIR.joinpath('sieve.txt').read_text())

    def test_keep_subscription_performs_no_network_request(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', None, None, False)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe') as execute:
            self.post(f'/unsubscribe/{row["id"]}', action='dismiss')
            execute.assert_not_called()

    def test_each_setting_persists_and_triggers_can_be_removed(self):
        for field, value in (('classify_mode', 'paid'), ('rule_model', 'gpt5'), ('reply_model', 'ling-free')):
            self.assertEqual(self.post('/settings', **{field: value}).status_code, 302)
            self.assertEqual(self.module.mailbox_settings.load_settings()[field], value)
        self.post('/add-reply-trigger', trigger_type='sender_email', value='Person@Example.com')
        self.assertTrue(self.module.mailbox_settings.matches_reply_trigger('person@example.com'))
        self.post('/remove-reply-trigger', trigger_type='sender_email', value='person@example.com')
        self.assertFalse(self.module.mailbox_settings.matches_reply_trigger('person@example.com'))

    def test_mark_reviewed_does_not_send_mail(self):
        self.module.tahor_db.create_reply_draft('id', 'thread', 'x@example.com', 'Test', 'Draft text', 'test')
        db = self.module.tahor_db.get_db()
        id = db.execute('SELECT id FROM reply_drafts').fetchone()['id']
        db.close()
        self.assertEqual(self.post(f'/dismiss-draft/{id}').status_code, 404)
        self.assertNotIn('Draft text', self.client.get('/drafts').get_data(as_text=True))


    def test_reply_rules_can_be_created_edited_paused_and_excluded(self):
        rule_module = self.module.reply_rules
        original = self.module.mailbox_settings.load_settings()
        self.addCleanup(self.module.mailbox_settings.save_settings, original)
        response = self.post('/reply-rules/save', name='Community updates', match_type='natural_language', match='Personal updates from community volunteers', instructions='Thank them and mention a request.', signature='Regards,\nExample Owner', max_sentences='3')
        self.assertEqual(response.status_code, 302)
        rule = next(r for r in rule_module.get_rules() if r['name']=='Community updates')
        self.module.tahor_db.record_reply_rule_match(rule['id'], '<sample@example.com>', 'volunteer@example.com')
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('Community updates', page)
        self.assertIn('volunteer@example.com', page)
        self.assertIn('Opt out', page)
        self.assertEqual(self.post('/reply-rules/exclude', rule_id=rule['id'], sender='volunteer@example.com', excluded='1').status_code, 302)
        self.assertIn('volunteer@example.com', next(r for r in rule_module.get_rules() if r['id']==rule['id'])['excluded_senders'])
        self.assertEqual(self.post('/reply-rules/toggle', rule_id=rule['id'], enabled='0').status_code, 302)
        self.assertFalse(any(r['id']==rule['id'] for r in rule_module.get_rules()))
        self.assertEqual(self.post('/reply-rules/save', rule_id=rule['id'], name='Edited', match_type='natural_language', match='Community mail', instructions='Reply to their questions.', signature='', max_sentences='2').status_code, 302)
        self.assertEqual(next(r for r in rule_module.get_rules() if r['id']==rule['id'])['max_sentences'], 2)
        self.assertEqual(self.post('/reply-rules/save', name='Bad', match_type='natural_language', match='', instructions='', max_sentences='9').status_code, 400)

    def test_reply_rule_content_is_escaped_and_forms_have_csrf(self):
        original = self.module.mailbox_settings.load_settings()
        self.addCleanup(self.module.mailbox_settings.save_settings, original)
        self.module.reply_rules.save_rule('<script>alert(1)</script>', 'natural_language', '<img src=x>', 'Say hello.', '<b>Owner</b>')
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertEqual(page.count('<form '), page.count('name="csrf_token"'))


    def test_unblock_updates_worker_and_sieve(self):
        self.module.tahor_db.set_sender_rule('blocked.example', 'block_all')
        self.module.generate_sieve.refresh_sieve()
        self.assertIn('blocked.example', self.client.get('/unsubscribe').get_data(as_text=True))
        self.assertEqual(self.post('/unblock-sender', domain='blocked.example').status_code, 302)
        self.assertIsNone(self.module.tahor_db.get_sender_rule('blocked.example'))
        self.assertNotIn('blocked.example', (self.root / 'sieve.txt').read_text())

    def test_message_keep_and_trash_apply_keywords_before_resolution(self):
        import keyword_tool
        for action, keyword in [('keep', 'retention-standard'), ('keep_brief', 'retention-short-lived'), ('trash', 'retention-transient')]:
            id = self.decision('message_review', {'mailbox': 'INBOX', 'message_id': '<review@example.com>'})
            with patch('message_reviews.locate', return_value={'uid': '12', 'uidvalidity': '8', 'sender': 'Sender <sender@example.com>', 'received_at': '2026-09-01T12:30:00+00:00'}), patch.object(keyword_tool, 'apply_ops', return_value={'applied': {'<review@example.com>'}}) as apply:
                self.assertEqual(self.post(f'/resolve/{id}', action=action).status_code, 302)
            operation = apply.call_args.args[0][0]
            self.assertIn(keyword, operation['add'])
            self.assertEqual(operation['delete'], action == 'trash')
            self.assertIn('retention-pending-review', operation['remove'])

    def test_sieve_dismissal_does_not_resolve_unrelated_decision(self):
        id = self.decision('message_review')
        self.post(f'/dismiss-sieve/{id}')
        db = self.module.tahor_db.get_db()
        self.assertEqual(db.execute('SELECT status FROM decisions WHERE id=?', (id,)).fetchone()['status'], 'pending')
        db.close()

    def test_preparing_draft_is_visible_but_cannot_be_marked_reviewed(self):
        self.module.tahor_db.prepare_reply_draft('preparing-id', 'preparing-thread', 'person@example.com', 'Saved later', 'Please retry this draft.', 'test')
        page = self.client.get('/drafts').get_data(as_text=True)
        self.assertEqual(self.client.get('/drafts').location, '/settings#reply-rules')
        self.assertNotIn('Please retry this draft.', page)
        self.assertNotIn('Mark reviewed', page)

    def test_status_requires_login_but_health_check_is_public(self):
        self.assertEqual(self.client.get('/status').status_code, 200)
        self.client.get('/logout')
        for route in ('/', '/settings', '/unsubscribe', '/drafts', '/status'):
            self.assertEqual(self.client.get(route).status_code, 302)
        self.assertEqual(self.client.get('/healthz').json, {'ok': True})

    def test_subscription_results_stay_with_sender_and_failed_request_is_retryable(self):
        from urllib.error import HTTPError
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', 'offers@example.com', 'Example offers', 'https://example.com/unsubscribe', None, True)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe', side_effect=HTTPError('https://example.com/private-token', 403, 'Forbidden', {}, None)):
            response = self.client.post(f'/unsubscribe/{row["id"]}', data={'action': 'unsubscribe', 'csrf_token': self.token()}, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result['pending'])
        self.assertTrue(result['failed'])
        self.assertEqual(result['candidate_id'], row['id'])
        self.assertNotIn('private-token', result['message'])
        self.assertEqual(self.module.tahor_db.get_unsubscribe_candidate('example.com')['status'], 'pending')
        page = self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertIn('Stop marketing, keep transactions', page)
        self.assertIn('Selected actions are queued. You can continue reviewing other senders.', page)
        self.assertIn('aria-live="polite"', page)

    def test_marketing_block_survives_unsubscribe_failure_without_claiming_success(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', 'https://example.com/unsubscribe', None, True)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe', side_effect=RuntimeError('unavailable')):
            response = self.client.post(f'/unsubscribe/{row["id"]}', data={'action': 'unsubscribe_block_marketing', 'csrf_token': self.token()}, headers={'Accept': 'application/json'})
        self.assertTrue(response.get_json()['failed'])
        self.assertIn('transactional mail remains allowed', response.get_json()['message'])
        self.assertEqual(self.module.tahor_db.get_sender_rule('example.com'), 'block_marketing')
