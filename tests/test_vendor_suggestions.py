import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import vendor_suggestions as suggestions


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
            'bucket': 'Shopping/Marketplace', 'vendor': 'General Marketplace', 'action': 'keep', 'reason': 'Specific purchase receipt'}})}
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
        suggestions.suggest_pending(model, limit=99)
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
        valid = {'bucket': 'Shopping', 'vendor': 'Store', 'action': 'review', 'reason': 'Ambiguous'}
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
            'action': 'keep', 'reason': 'Specific receipt'}})}
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
