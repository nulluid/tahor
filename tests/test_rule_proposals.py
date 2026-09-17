import json
from unittest.mock import patch
from test_web_security import AppTestCase

class RuleProposalTests(AppTestCase):
    def post(self, path, **data):
        return self.client.post(path, data=dict(data, csrf_token=self.token()))

    def propose(self, text, result):
        with patch.object(self.module.apply_decisions, 'rule_model_call', return_value=result):
            self.post('/add-rule', rule_text=text)
        db = self.module.tahor_db.get_db()
        row = db.execute("SELECT * FROM decisions WHERE kind='free_text_rule' ORDER BY id DESC LIMIT 1").fetchone()
        db.close()
        return row['id'], json.loads(row['context']) if row['context'].startswith('{') else {}

    def test_wrong_parent_brand_and_email_only_targets_never_apply(self):
        for text, domain in [('Block alerts.example', 'example.com'), ('Block alerts.example.com', 'example.com'), ('Block Acme', 'acme.example'), ('Block person@alerts.example', 'alerts.example')]:
            with patch.object(self.module.apply_decisions, 'apply_sender_rule') as apply:
                self.propose(text, {'kind':'sender_rule','sender_rule':{'domain':domain,'rule':'block_all'}})
                apply.assert_not_called()

    def test_sender_proposal_requires_approval_and_replay_is_idempotent(self):
        result = {'kind':'sender_rule','sender_rule':{'domain':'alerts.example','rule':'block_all','attempt_unsubscribe':False}}
        with patch.object(self.module.apply_decisions, 'apply_sender_rule', return_value='blocked') as apply:
            id, ctx = self.propose('Block alerts.example.', result)
            apply.assert_not_called()
            token = ctx['rule_proposal']['token']
            self.post(f'/review-rule/{id}', action='approve', proposal=token)
            self.post(f'/review-rule/{id}', action='approve', proposal=token)
            apply.assert_called_once_with(result['sender_rule'])

    def test_preview_escaped_reject_and_stale_token_do_not_write(self):
        path = self.root/'prompt.txt';path.write_text('original')
        id, ctx = self.propose('Add a policy', {'kind':'file_edit','prompt_txt':'<script>alert(1)</script>'})
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert(1)</script>',page)
        self.assertEqual(self.post(f'/review-rule/{id}',action='approve',proposal='stale').status_code,409)
        self.post(f'/review-rule/{id}',action='reject',proposal=ctx['rule_proposal']['token'])
        self.assertEqual(path.read_text(),'original')

    def test_concurrent_file_edit_stops_approved_proposal(self):
        path = self.root/'prompt.txt';path.write_text('original')
        id,ctx = self.propose('Change policy',{'kind':'file_edit','prompt_txt':'proposed'})
        path.write_text('new owner edit')
        self.post(f'/review-rule/{id}',action='approve',proposal=ctx['rule_proposal']['token'])
        self.assertEqual(path.read_text(),'new owner edit')
        self.assertIn('Rules changed since this preview', self.client.get('/').get_data(as_text=True))

    def test_partial_write_retry_completes_saved_proposal_without_model(self):
        prompt=self.root/'prompt.txt';prompt.write_text('original')
        buckets=self.root/'vendor_buckets.json';buckets.write_text('{}')
        id,ctx=self.propose('Change both',{'kind':'file_edit','prompt_txt':'proposed','vendor_buckets_json':'{"shop":["Shopping","Shop"]}'})
        original=self.module.apply_decisions.atomic_write
        def fail_prompt(path,value):
            if path == prompt: raise OSError('temporary failure')
            return original(path,value)
        with patch.object(self.module.apply_decisions,'atomic_write',side_effect=fail_prompt):
            self.post(f'/review-rule/{id}',action='approve',proposal=ctx['rule_proposal']['token'])
        self.assertEqual(prompt.read_text(),'original')
        self.assertIn('shop',json.loads(buckets.read_text()))
        with patch.object(self.module.apply_decisions,'rule_model_call') as model:
            self.post(f'/retry-rule/{id}')
            model.assert_not_called()
        self.assertEqual(prompt.read_text(),'proposed')

    def test_free_mode_disabled_is_visible_and_does_not_switch_to_paid(self):
        original=self.module.mailbox_settings.get_classify_mode()
        with patch.dict('os.environ',{'TAHOR_CLASSIFY_FREE_ENABLED':'0'}):
            self.assertIn('Free classification is disabled',self.client.get('/settings').get_data(as_text=True))
            self.assertEqual(self.post('/settings',classify_mode='free').status_code,400)
            self.assertEqual(self.module.mailbox_settings.get_classify_mode(),original)

    def test_review_requires_authentication_and_csrf(self):
        id, ctx = self.propose('Change policy', {'kind':'file_edit','prompt_txt':'proposed'})
        data={'action':'approve','proposal':ctx['rule_proposal']['token']}
        self.assertEqual(self.client.post(f'/review-rule/{id}',data=data).status_code,400)
        with self.client.session_transaction() as session:
            session.pop('email',None)
        self.assertIn(self.client.post(f'/review-rule/{id}',data=dict(data,csrf_token=self.token())).status_code,(302,403))
