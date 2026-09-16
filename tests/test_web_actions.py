import json
from unittest.mock import patch
from test_web_security import AppTestCase


class WebActionTests(AppTestCase):
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
        self.assertEqual((self.root / 'prompt.txt').read_text(), 'Keep receipts.')
        self.assertNotIn('Retry rule', self.client.get('/').get_data(as_text=True))

    def test_unsubscribe_failure_stays_pending_and_is_visible(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', 'https://example.com/unsubscribe', None, True)
        row = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        with patch.object(self.module.tahor_db, 'execute_unsubscribe', side_effect=RuntimeError('unreachable')):
            self.post(f'/unsubscribe/{row["id"]}', action='unsubscribe')
        self.assertEqual(self.module.tahor_db.get_unsubscribe_candidate('example.com')['status'], 'pending')
        self.assertIn('Unsubscribe failed', self.client.get('/unsubscribe').get_data(as_text=True))

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
        for field, value in (('classify_mode', 'paid'), ('rule_model', 'gpt5'), ('reply_model', 'nemotron-free')):
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
        self.assertEqual(self.post(f'/dismiss-draft/{id}').status_code, 302)
        self.assertNotIn('Draft text', self.client.get('/drafts').get_data(as_text=True))
