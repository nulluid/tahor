import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import draft_replies
import mailbox_settings
import reply_backend_recovery
import tahor_db


class ReplyFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, name, value in ((tahor_db, 'DB_PATH', self.root/'db.sqlite'),
                                    (mailbox_settings, 'SETTINGS_PATH', self.root/'settings.json')):
            change = patch.object(target, name, value)
            change.start()
            self.addCleanup(change.stop)
        env = patch.dict(os.environ, {'OPENROUTER_API_KEY': 'private-test-key', 'FASTMAIL_EMAIL': 'owner@example.com'})
        env.start()
        self.addCleanup(env.stop)
        mailbox_settings.set_reply_model('gpt5')
        mailbox_settings.set_reply_backup_model('nemotron-free')
        self.generated = json.dumps({'sentences': ['Thank you for the update.']})
        self.approved = json.dumps({'approved': True, 'issues': [], 'needs_attention': False})
        self.rule = {'instructions': 'Thank the writer.', 'signature': 'Regards,\nExample Owner', 'max_sentences': 3}

    def write(self):
        return draft_replies.draft_reply_body('Update', 'person@example.org', 'The project is complete.', self.rule)

    def test_primary_credit_failure_uses_free_writer_and_verifier(self):
        failure = urllib.error.HTTPError('https://example.org', 402, 'private error', {}, io.BytesIO(b'synthetic failure'))
        with patch.object(draft_replies, 'reply_completion', side_effect=[failure, self.generated, self.approved]) as complete:
            self.assertIn('Thank you', self.write())
        models = [call.args[0]['model'] for call in complete.call_args_list]
        self.assertEqual(models, ['openai/gpt-5.1', 'nvidia/nemotron-3-super-120b-a12b:free', 'nvidia/nemotron-3-super-120b-a12b:free'])
        path = reply_backend_recovery.state_path()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private', path.read_text())
        self.assertNotIn('project', path.read_text())

    def test_cooldown_survives_reload_then_primary_recovers(self):
        with patch.object(reply_backend_recovery.time, 'time', return_value=1000), patch.object(draft_replies, 'reply_completion', side_effect=[TimeoutError(), self.generated, self.approved]):
            self.write()
        importlib.reload(reply_backend_recovery)
        with patch.object(reply_backend_recovery.time, 'time', return_value=1100), patch.object(draft_replies, 'reply_completion', side_effect=[self.generated, self.approved]) as complete:
            self.write()
        self.assertTrue(all(call.args[0]['model'].endswith(':free') for call in complete.call_args_list))
        with patch.object(reply_backend_recovery.time, 'time', return_value=1301), patch.object(draft_replies, 'reply_completion', side_effect=[self.generated, self.approved]) as complete:
            self.write()
        self.assertTrue(all(call.args[0]['model'] == 'openai/gpt-5.1' for call in complete.call_args_list))
        self.assertEqual(json.loads(reply_backend_recovery.state_path().read_text()), {})

    def test_both_fail_stays_retryable_and_free_quality_rejection_is_not_success(self):
        with patch.object(draft_replies, 'reply_completion', side_effect=TimeoutError()), self.assertRaises(ValueError):
            self.write()
        self.assertEqual(len(json.loads(reply_backend_recovery.state_path().read_text())), 2)
        reply_backend_recovery.state_path().unlink()
        rejected = json.dumps({'approved': False, 'issues': ['Unsupported assertion.'], 'needs_attention': False})
        with patch.object(draft_replies, 'reply_completion', side_effect=[TimeoutError(), self.generated, rejected, self.generated, rejected]), patch.object(reply_backend_recovery, 'record_success') as success, self.assertRaisesRegex(ValueError, 'verification'):
            self.write()
        success.assert_not_called()

    def test_quality_rejection_does_not_bypass_review_by_switching_provider(self):
        rejected = json.dumps({'approved': False, 'issues': ['Unsupported assertion.'], 'needs_attention': False})
        with patch.object(draft_replies, 'reply_completion', side_effect=[self.generated, rejected]*2) as complete, self.assertRaisesRegex(ValueError, 'verification'):
            self.write()
        self.assertTrue(all(call.args[0]['model'] == 'openai/gpt-5.1' for call in complete.call_args_list))

    def test_fully_free_configuration_never_calls_paid_model(self):
        mailbox_settings.set_reply_model('nemotron-free')
        with patch.object(draft_replies, 'reply_completion', side_effect=TimeoutError()) as complete, self.assertRaises(ValueError):
            self.write()
        self.assertEqual(complete.call_count, 1)
        self.assertTrue(complete.call_args.args[0]['model'].endswith(':free'))
        with patch.object(draft_replies, 'reply_completion') as complete, self.assertRaises(ValueError):
            self.write()
        complete.assert_not_called()

    def test_disabled_backup_makes_no_free_request_and_primary_recovers(self):
        mailbox_settings.set_reply_backup_model('none')
        self.assertEqual(mailbox_settings.get_reply_backup_model(), 'none')
        with patch.object(reply_backend_recovery.time, 'time', return_value=1000), patch.object(draft_replies, 'reply_completion', side_effect=TimeoutError()) as complete, self.assertRaises(ValueError):
            self.write()
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(complete.call_args.args[0]['model'], 'openai/gpt-5.1')
        with patch.object(reply_backend_recovery.time, 'time', return_value=1100), patch.object(draft_replies, 'reply_completion') as complete, self.assertRaises(ValueError):
            self.write()
        complete.assert_not_called()
        with patch.object(reply_backend_recovery.time, 'time', return_value=1301), patch.object(draft_replies, 'reply_completion', side_effect=[self.generated, self.approved]) as complete:
            self.write()
        self.assertTrue(all(call.args[0]['model'] == 'openai/gpt-5.1' for call in complete.call_args_list))

    def test_backup_setting_rejects_paid_models_and_corruption_defaults_free(self):
        for key in ('gpt5', 'gpt5-flex', 'gemini-flash', 'missing'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                mailbox_settings.set_reply_backup_model(key)
        settings = mailbox_settings.load_settings()
        settings['reply_backup_model'] = 'gpt5'
        mailbox_settings.save_settings(settings)
        self.assertEqual(mailbox_settings.get_reply_backup_model(), 'nemotron-free')
