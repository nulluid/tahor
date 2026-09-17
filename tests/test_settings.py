import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('settings_under_test', ROOT / 'mailbox_settings.py')
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'
        self.patch = patch.object(settings, 'SETTINGS_PATH', self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_concurrent_writers_preserve_all_updates(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: settings.add_reply_trigger('sender_email', f'user{i}@example.com'), range(40)))
        self.assertEqual(len(settings.get_reply_triggers()), 40)
        settings.set_classify_mode('paid')
        settings.set_backlog_estimate(100)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: settings.decrement_backlog_estimate(1), range(40)))
        self.assertEqual(settings.get_cached_backlog(60)[0], 60)
        self.assertEqual(settings.get_classify_mode(), 'paid')
        self.assertEqual(len(settings.get_reply_triggers()), 40)

    def test_default_lists_do_not_leak_between_loads(self):
        one = settings.load_settings()
        one['reply_triggers'].append({'type': 'sender_email', 'value': 'x@example.com'})
        self.assertEqual(settings.load_settings()['reply_triggers'], [])

    def test_nonobject_json_is_handled(self):
        self.path.write_text('[]')
        self.assertEqual(settings.get_classify_mode(), 'free')

    def test_independent_ai_policies_validate_tiers_and_preserve_other_tasks(self):
        settings.set_ai_task_settings('reply', 'free', 'grok-4.6', 'ling-free')
        settings.set_ai_task_settings('rule', 'paid_only', 'grok-4.6', 'ling-free')
        settings.set_ai_task_settings('classification', 'auto')
        self.assertEqual(settings.get_ai_policy('reply'), 'free')
        self.assertEqual(settings.get_ai_policy('rule'), 'paid_only')
        self.assertEqual(settings.get_ai_policy('classification'), 'auto')
        self.assertEqual(settings.get_ai_models('reply'), {'paid': 'grok-4.6', 'free': 'ling-free'})
        before = self.path.read_text()
        for args in [('reply', 'free', 'ling-free', 'grok-4.6'),
                     ('rule', 'paid', 'grok-4.6', 'none'),
                     ('unknown', 'free', None, None), ('reply', 'unknown', None, None)]:
            with self.assertRaises(ValueError):
                settings.set_ai_task_settings(*args)
            self.assertEqual(self.path.read_text(), before)
        settings.set_ai_task_settings('reply', 'free', 'none', 'ling-free')
        self.assertFalse(settings.is_ai_enabled('reply'))
        self.assertTrue(settings.is_ai_enabled('rule'))

    def test_legacy_free_primary_and_paid_backup_keep_their_spending_permission(self):
        self.path.write_text(json.dumps({'reply_model': 'ling-free'}))
        self.assertEqual(settings.get_ai_policy('reply'), 'free')
        self.assertEqual(settings.get_ai_models('reply')['free'], 'ling-free')
        self.path.write_text(json.dumps({'reply_model': 'grok-4.6', 'reply_backup_model': 'ling-free'}))
        self.assertEqual(settings.get_ai_policy('reply'), 'paid')
        settings.set_ai_policy('reply', 'paid_only')
        self.assertEqual(settings.get_ai_policy('reply'), 'paid_only')

    def test_legacy_setters_do_not_reverse_explicit_free_or_disabled_fallback(self):
        for task in ('reply', 'rule'):
            settings.set_ai_task_settings(task, 'paid_only', 'grok-4.6', 'ling-free')
            getattr(settings, 'set_' + task + '_model')('ling-free')
            self.assertEqual(settings.get_ai_policy(task), 'free')
        settings.set_ai_task_settings('reply', 'paid', 'grok-4.6', 'ling-free')
        settings.set_reply_backup_model('none')
        self.assertEqual(settings.get_ai_policy('reply'), 'paid_only')
        settings.set_reply_model('ling-free')
        settings.set_reply_backup_model('none')
        self.assertEqual(settings.get_ai_policy('reply'), 'free')

    def test_auto_backlog_threshold_is_four_hours(self):
        self.assertEqual(settings.ESCALATION_TARGET_SECONDS, 14400)
        self.assertEqual(settings.decide_backend_split(2880, 0.2, 50), (50, 0))
        free, paid = settings.decide_backend_split(2881, 0.2, 50)
        self.assertGreater(paid, 0)

    def test_invalid_triggers_rejected(self):
        for value in ('"bad"@example.com', 'x@example.com\r\nINJECT', 'missing-domain'):
            with self.assertRaises(ValueError):
                settings.add_reply_trigger('sender_email', value)
        self.assertEqual(settings.get_reply_triggers(), [])


if __name__ == '__main__':
    unittest.main()
