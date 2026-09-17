import importlib.util
import json
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "decision-app"))
import unittest
from unittest.mock import patch, MagicMock
import draft_replies
import mailbox_settings
from model_privacy import private_request_payload

spec = importlib.util.spec_from_file_location('privacy_apply', Path(__file__).resolve().parents[1] / 'decision-app' / 'apply_decisions.py')
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)

class WritingPrivacyTests(unittest.TestCase):
    def test_options_cannot_weaken_privacy(self):
        backend = {'url': 'https://openrouter.ai/api/v1/chat/completions', 'request_options': {'provider': {'only': ['example'], 'zdr': False, 'data_collection': 'allow'}}}
        self.assertEqual(private_request_payload(backend, {})['provider'], {'only': ['example'], 'zdr': True, 'data_collection': 'deny'})

    def test_disabled_and_direct_routes_make_no_network_request(self):
        with patch.object(draft_replies.urllib.request, 'urlopen') as network:
            with patch.object(mailbox_settings, 'get_reply_model', return_value='none'), self.assertRaisesRegex(ValueError, 'disabled'):
                draft_replies.draft_reply_body('subject', 'sender@example.com', 'body')
            with patch.object(mailbox_settings, 'get_rule_model', return_value='none'), self.assertRaisesRegex(ValueError, 'disabled'):
                apply.rule_model_call('private directions')
            with self.assertRaisesRegex(ValueError, 'privacy'):
                draft_replies.reply_completion({'url':'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'}, 'secret', {})
            network.assert_not_called()

    def test_rule_request_enforces_private_route(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read1.side_effect = [json.dumps({'choices':[{'message':{'content':'{}'}}]}).encode(), b'']
        with patch.object(mailbox_settings, 'get_rule_model', return_value='gpt5'), patch.dict(apply.os.environ, {'OPENROUTER_API_KEY':'synthetic'}), patch.object(apply.urllib.request, 'urlopen', return_value=response) as network:
            apply.rule_model_call('synthetic directions')
        self.assertEqual(json.loads(network.call_args.args[0].data)['provider'], {'zdr':True, 'data_collection':'deny'})

    def test_retired_or_missing_selections_disable_without_paid_substitution(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(mailbox_settings, 'SETTINGS_PATH', Path(temporary)/'settings.json'):
            self.assertEqual(mailbox_settings.get_reply_model(), 'none')
            mailbox_settings.SETTINGS_PATH.write_text(json.dumps({'rule_model':'nemotron-free','reply_model':'gemini-flash','reply_backup_model':'nemotron-free'}))
            self.assertEqual(mailbox_settings.get_rule_model(), 'none')
            self.assertEqual(mailbox_settings.get_reply_model(), 'none')
            self.assertEqual(mailbox_settings.get_reply_backup_model(), 'none')
