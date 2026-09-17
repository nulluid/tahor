import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import vendor_suggestions as suggestions


def evidence(**changes):
    return dict(confidence=0.96, merchant_identified=True, samples_consistent=True, routine_transaction=True, shared_sender=False, **changes)


class VendorSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'decisions.db'
        conn = self.db()
        conn.execute('CREATE TABLE decisions (id INTEGER PRIMARY KEY,kind TEXT,status TEXT,context TEXT,resolution TEXT)')
        conn.commit(); conn.close()
        self.start(patch.object(suggestions.tahor_db, 'get_db', side_effect=self.db))
        self.enabled = self.start(patch.object(suggestions.mailbox_settings, 'is_ai_enabled', return_value=True))
        self.start(patch.object(suggestions.config, 'vendor_buckets', return_value={'other.example': ['Shopping/Marketplace', 'Marketplace']}))
        self.record = self.start(patch.object(suggestions.ai_routing, 'record_result'))
        self.now = self.start(patch.object(suggestions.time, 'time', return_value=1000))

    def start(self, p):
        value = p.start(); self.addCleanup(p.stop); return value

    def db(self):
        conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def add(self, number=1):
        context = dict(sender_email=f'store{number}@delivery.example', routing_key=f'store{number}@delivery.example',
                       display_name='General Marketplace', subject='Gym equipment order', samples=[{'subject': 'Gym equipment order'}])
        with self.db() as conn:
            conn.execute('INSERT INTO decisions VALUES (?, ?, ?, ?, NULL)', (number, 'vendor_mapping', 'pending', json.dumps(context)))

    def get(self, number=1):
        with self.db() as conn:
            return conn.execute('SELECT * FROM decisions WHERE id=?', (number,)).fetchone()

    def model(self, prompt, **kwargs):
        data = json.loads(prompt.split('\n', 1)[1])
        self.assertIn('untrusted', prompt)
        self.assertIn('marketplace', prompt.lower())
        proposal = {'kind': 'file_edit', 'vendor_buckets_json': json.dumps({data['exact_sender']: {
            'bucket': 'Shopping/Marketplace', 'vendor': 'General Marketplace', 'action': 'keep', 'reason': 'Specific purchase receipt', **evidence()}})}
        kwargs['validate'](proposal)
        return proposal

    def test_suggestion_is_reviewable_and_does_not_resolve_or_apply_rule(self):
        self.add()
        self.assertEqual(suggestions.suggest_pending(self.model), 0)
        row = self.get(); context = json.loads(row['context'])
        self.assertEqual(row['status'], 'pending'); self.assertIsNone(row['resolution'])
        self.assertEqual(context['suggested_bucket'], 'Shopping/Marketplace')
        self.assertEqual(context['suggested_action'], 'keep')
        self.assertEqual(context['suggestion_source'], 'ai')
        with patch.object(suggestions, '_validated') as validate:
            suggestions.suggest_pending(self.model)
            validate.assert_not_called()

    def test_at_most_three_calls_and_disabled_writer_never_calls(self):
        for number in range(1, 6): self.add(number)
        calls = []
        def model(prompt, **kwargs):
            calls.append(kwargs['work_id']); return self.model(prompt, **kwargs)
        suggestions.suggest_pending(model)
        self.assertEqual(len(calls), 3)
        self.enabled.return_value = False
        suggestions.suggest_pending(model)
        self.assertEqual(len(calls), 3)

    def test_failure_persists_retry_deadline_and_other_rows_are_not_starved(self):
        self.add(1); self.add(2)
        def fail(*args, **kwargs): raise OSError('provider unavailable')
        self.assertEqual(suggestions.suggest_pending(fail, limit=1), 1)
        self.assertEqual(json.loads(self.get()['context'])['suggestion_retry_at'], 1300)
        suggestions.suggest_pending(self.model, limit=1)
        self.assertEqual(json.loads(self.get(2)['context'])['suggestion_status'], 'ready')
        with self.db() as conn:
            self.assertEqual(suggestions.pending_work_ids(conn), ['vendor:1'])

    def test_concurrent_owner_choice_is_never_overwritten(self):
        self.add()
        def model(prompt, **kwargs):
            with self.db() as conn:
                conn.execute("UPDATE decisions SET status='resolved',resolution='{}' WHERE id=1")
            return self.model(prompt, **kwargs)
        suggestions.suggest_pending(model)
        row = self.get()
        self.assertEqual(row['status'], 'resolved')
        self.assertNotIn('suggestion_source', json.loads(row['context']))

    def test_rejects_prompt_mutations_scope_broadening_and_invalid_targets(self):
        key = 'store1@delivery.example'
        valid = {'bucket': 'Shopping', 'vendor': 'Store', 'action': 'review', 'reason': 'Ambiguous', **evidence()}
        cases = [dict(kind='file_edit', vendor_buckets_json=json.dumps({'delivery.example': valid})),
                 dict(kind='file_edit', vendor_buckets_json=json.dumps({key: valid}), prompt_txt='change rules'),
                 dict(kind='sender_rule', sender_rule={'domain': 'delivery.example'}),
                 dict(kind='file_edit', vendor_buckets_json=json.dumps({key: dict(valid, bucket='../Trash')})),
                 dict(kind='file_edit', vendor_buckets_json=json.dumps({key: dict(valid, action='block_all')}))]
        for proposal in cases:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                suggestions._validated(proposal, key)

    def test_dedicated_schema_uses_existing_private_rule_route(self):
        from test_grok_rule_route import apply
        key = 'store1@delivery.example'
        proposal = {'kind': 'file_edit', 'vendor_buckets_json': json.dumps({key: {
            'bucket': 'Shopping/Marketplace', 'vendor': 'General Marketplace',
            'action': 'keep', 'reason': 'Specific receipt', **evidence()}})}
        response = io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(proposal)}}]}).encode())
        def route(task, registry, generate, **kwargs):
            self.assertEqual(task, 'rule')
            self.assertEqual(kwargs['work_id'], 'vendor:1')
            return generate('grok-4.6')
        with patch('ai_routing.run', side_effect=route), patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic'}), \
             patch.object(apply.urllib.request, 'urlopen', return_value=response) as network:
            result = apply.rule_model_call('Synthetic observed sender headers', work_id='vendor:1',
                system_prompt=suggestions.SYSTEM_PROMPT, validate=lambda item: suggestions._validated(item, key))
        self.assertEqual(result, proposal)
        payload = json.loads(network.call_args.args[0].data)
        self.assertEqual(payload['messages'][0]['content'], suggestions.SYSTEM_PROMPT)
        self.assertTrue(payload['provider']['zdr'])
        self.assertEqual(payload['provider']['data_collection'], 'deny')
        self.assertIn('xai/zdr', payload['provider']['only'])

    def test_confident_routine_mapping_applies_exact_sender_without_owner_click(self):
        from test_grok_rule_route import apply
        self.add()
        path = Path(self.tmp.name) / 'vendor_buckets.json'
        path.write_text('{"existing.example":["Shopping","Existing"]}')
        def apply_decision(identifier):
            row = self.get(identifier)
            choice = json.loads(row['resolution'])
            self.assertEqual(choice['action'], 'map')
            self.assertTrue(choice['automatic_vendor_mapping'])
            with patch.object(apply, 'VENDOR_BUCKETS_PATH', path):
                apply.apply_vendor_mapping(dict(context=row['context'], summary='Vendor: delivery.example'), choice)
        suggestions.suggest_pending(self.model, apply_decision=apply_decision)
        self.assertEqual(self.get()['status'], 'resolved')
        self.assertEqual(json.loads(path.read_text()), {'existing.example': ['Shopping', 'Existing'],
            'store1@delivery.example': ['Shopping/Marketplace', 'General Marketplace']})
        self.record.assert_called_with('rule', 'vendor:1', True)

    def test_uncertain_nonroutine_conflicting_shared_sender_or_trash_never_autoapplies(self):
        for index, change in enumerate([{'confidence': 0.89}, {'action': 'trash'}, {'action': 'review'},
                {'merchant_identified': False}, {'samples_consistent': False},
                {'routine_transaction': False}, {'shared_sender': True}], 1):
            self.add(index)
            def model(prompt, **kwargs):
                proposal = self.model(prompt, **kwargs)
                mapping = json.loads(proposal['vendor_buckets_json'])
                next(iter(mapping.values())).update(change)
                proposal['vendor_buckets_json'] = json.dumps(mapping)
                return proposal
            with patch.object(suggestions, '_apply') as apply:
                suggestions.suggest_pending(model, apply_decision=lambda identifier: None)
                apply.assert_not_called()
            self.assertEqual(self.get(index)['status'], 'pending')
            self.assertIsNone(self.get(index)['resolution'])

    def test_existing_exact_mapping_is_reused_without_a_model_request(self):
        self.add()
        with patch.object(suggestions.config, 'vendor_buckets', return_value={'store1@delivery.example': ['Shopping', 'Existing Store']}), \
             patch.object(suggestions, '_validated') as validate:
            suggestions.suggest_pending(self.model, apply_decision=lambda identifier: None)
            validate.assert_not_called()
        self.assertEqual(json.loads(self.get()['resolution'])['vendor_name'], 'Existing Store')

    def test_nonfinite_boolean_or_out_of_range_confidence_is_rejected(self):
        key = 'store1@delivery.example'
        for confidence in (True, float('nan'), -1, 1.1):
            value = dict(bucket='Shopping', vendor='Store', action='keep', reason='Receipt', **evidence())
            value['confidence'] = confidence
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                suggestions._validated({'kind': 'file_edit', 'vendor_buckets_json': json.dumps({key: value})}, key)

    def test_autoapply_failure_remains_visible_and_retries_without_another_model_call(self):
        self.add()
        def fail(identifier): raise OSError('configuration temporarily unavailable')
        self.assertEqual(suggestions.suggest_pending(self.model, apply_decision=fail), 1)
        row = self.get(); self.assertEqual(row['status'], 'pending')
        self.assertTrue(json.loads(row['resolution'])['automatic_vendor_mapping'])
        with self.db() as conn:
            self.assertEqual(suggestions.pending_work_ids(conn), ['vendor:1'])
        def recover(identifier):
            with self.db() as conn:
                conn.execute("UPDATE decisions SET status='resolved' WHERE id=?", (identifier,))
        with patch.object(suggestions, '_validated') as validate:
            self.assertEqual(suggestions.suggest_pending(self.model, apply_decision=recover), 0)
            validate.assert_not_called()
        self.assertEqual(self.get()['status'], 'resolved')

    def test_manual_metadata_refresh_during_generation_preserves_newer_context(self):
        self.add()
        def model(prompt, **kwargs):
            context = json.loads(self.get()['context']); context['subject'] = 'Newer receipt'
            with self.db() as conn:
                conn.execute('UPDATE decisions SET context=? WHERE id=1', (json.dumps(context),))
            return self.model(prompt, **kwargs)
        suggestions.suggest_pending(model)
        self.assertEqual(json.loads(self.get()['context'])['subject'], 'Newer receipt')
        self.assertNotIn('suggestion_source', json.loads(self.get()['context']))
