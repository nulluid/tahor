import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import mailbox_settings

APP_DIR = Path(__file__).resolve().parents[1] / 'decision-app'
spec = importlib.util.spec_from_file_location('grok_rule_apply', APP_DIR / 'apply_decisions.py')
apply = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', [str(APP_DIR)] + sys.path):
    spec.loader.exec_module(apply)


class GrokRuleRouteTests(unittest.TestCase):
    def test_rule_call_uses_benchmarked_private_route_and_full_output_budget(self):
        expected = {'kind': 'needs_code_change', 'explanation': 'Requires another action.',
                    'sender_rule': None, 'vendor_buckets_json': None, 'prompt_txt': None}
        response = io.BytesIO(json.dumps({'choices': [{'message': {
            'content': json.dumps(expected),
        }}]}).encode())
        with patch.object(mailbox_settings, 'get_rule_model', return_value='grok-4.6'), patch.dict(
                os.environ, {'OPENROUTER_API_KEY': 'synthetic'}), patch.object(
                apply.urllib.request, 'urlopen', return_value=response) as network:
            result = apply._rule_model_call('Synthetic owner instructions.', 'grok-4.6')
        self.assertEqual(result, expected)
        payload = json.loads(network.call_args.args[0].data)
        self.assertEqual(payload['model'], 'x-ai/grok-4.6')
        self.assertEqual(payload['max_tokens'], 4096)
        self.assertEqual(payload['temperature'], 0.1)
        self.assertEqual(payload['reasoning'], {'effort': 'low'})
        self.assertEqual(payload['provider'], {'only': ['xai/zdr'], 'allow_fallbacks': False,
                                              'zdr': True, 'data_collection': 'deny'})
        self.assertEqual(payload['messages'][0]['content'], apply.RULE_DRAFTING_SYSTEM_PROMPT)
        self.assertEqual(payload['messages'][1]['content'], 'Synthetic owner instructions.')

    def test_rule_route_is_independent_from_reply_token_budget(self):
        self.assertEqual(mailbox_settings.RULE_MODELS['grok-4.6']['request_options']['max_tokens'], 4096)
        self.assertEqual(mailbox_settings.REPLY_MODELS['grok-4.6']['request_options']['max_tokens'], 2048)
        self.assertEqual(mailbox_settings.DEFAULT_RULE_MODEL, 'none')
