import json
from unittest.mock import patch
from test_web_security import AppTestCase


class RuleClarificationTests(AppTestCase):
    def add(self, text, result):
        module = self.module.apply_decisions
        with patch.object(module, 'rule_model_call', return_value=result):
            self.client.post('/add-rule', data={'rule_text': text, 'csrf_token': self.token()})
        database = self.module.tahor_db.get_db()
        try:
            return database.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            database.close()

    def test_brand_rule_becomes_clarification_without_actions_or_repeat_requests(self):
        module = self.module.apply_decisions
        proposal = {'kind': 'sender_rule', 'sender_rule': {'domain': 'acme.example',
                    'rule': 'block_marketing', 'attempt_unsubscribe': True}}
        with patch.object(module, 'apply_sender_rule') as apply:
            row = self.add('Block Acme marketing; keep receipts and unsubscribe.', proposal)
            context = json.loads(row['context'])
            self.assertEqual(row['status'], 'pending')
            self.assertIn('rule_clarification', context)
            self.assertNotIn('rule_proposal', context)
            self.assertFalse(context.get('applied'))
            self.assertIn('Block Acme', json.loads(row['resolution'])['text'])
            with patch.object(module, 'rule_model_call') as model:
                module.apply_one(row['id'])
                module.main()
                model.assert_not_called()
            apply.assert_not_called()

    def test_clarification_schema_cannot_smuggle_actions_and_requires_explanation(self):
        validate = self.module.apply_decisions.validate_rule_proposal
        validate({'kind': 'needs_clarification', 'explanation': 'Specify the scope.'})
        for invalid in ({'kind': 'needs_clarification'},
                        {'kind': 'needs_clarification', 'explanation': ''},
                        {'kind': 'needs_clarification', 'explanation': 'Question', 'prompt_txt': 'Edit'},
                        {'kind': 'needs_clarification', 'explanation': 'Question', 'sender_rule': {}},
                        {'kind': 'needs_clarification', 'explanation': 'Question', 'vendor_buckets_json': '{}'}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate(invalid)

    def test_model_question_is_not_persisted_and_old_failure_marker_is_cleared(self):
        import ai_routing
        with patch.object(ai_routing, 'record_result') as reconcile:
            row = self.add('Clarify my rule.', {'kind': 'needs_clarification', 'explanation': 'PRIVATE MODEL CLAIM'})
        self.assertNotIn('PRIVATE MODEL CLAIM', row['context'])
        reconcile.assert_called_once_with('rule', row['id'], True)
        database = self.module.tahor_db.get_db()
        try:
            self.assertNotIn(row['id'], self.module.apply_decisions.pending_ai_rule_ids(database))
            with database:
                database.execute("UPDATE decisions SET status='resolved' WHERE id=?", (row['id'],))
        finally:
            database.close()
        with patch.object(self.module.apply_decisions, 'apply_one') as apply:
            self.module.apply_decisions.main()
            apply.assert_not_called()

    def test_domain_guard_failure_becomes_successful_clarification_in_router(self):
        module = self.module.apply_decisions
        proposal = {'kind': 'sender_rule', 'sender_rule': {'domain': 'acme.example',
                    'rule': 'block_all', 'attempt_unsubscribe': False}}
        observed = []
        def route(task, registry, operation, **kwargs):
            result = operation('grok-4.6')
            observed.append(result)
            return result
        with patch.object(module, '_rule_model_call', return_value=proposal), patch('ai_routing.run', side_effect=route):
            result = module.rule_model_call('Synthetic instruction', validate=lambda p:
                module.validate_explicit_sender_target('Block Acme', p['sender_rule']))
        self.assertEqual(result['kind'], 'needs_clarification')
        self.assertNotIn('sender_rule', result)
        self.assertEqual(observed, [result])
