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

    def test_invalid_triggers_rejected(self):
        for value in ('"bad"@example.com', 'x@example.com\r\nINJECT', 'missing-domain'):
            with self.assertRaises(ValueError):
                settings.add_reply_trigger('sender_email', value)
        self.assertEqual(settings.get_reply_triggers(), [])


if __name__ == '__main__':
    unittest.main()
