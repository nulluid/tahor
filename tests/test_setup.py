import argparse
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import setup_tahor
import run
import doctor
import fetch_batch
import tahor_db
import mailbox_settings


class SetupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd parser is available on Linux')
    def test_generated_units_pass_the_systemd_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_tahor.write_units(root, root / 'config.env', Path(sys.executable))
            result = subprocess.run(['systemd-analyze', 'verify', *map(str, root.iterdir())], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_doctor_accepts_imaplib_capabilities_and_custom_data_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / 'custom-prompt.txt'
            mapping = root / 'custom-map.json'
            prompt.write_text('test prompt')
            mapping.write_text('{}')
            conn = Mock(capabilities=('IMAP4REV1', 'MOVE', 'UIDPLUS'))
            conn.select.return_value = ('OK', [])
            values = dict(FASTMAIL_EMAIL='owner@example.com', FASTMAIL_APP_PASSWORD='fake', OPENROUTER_API_KEY='fake', DATA_DIR=directory, TAHOR_DB_PATH=str(root / 'test.db'), PROMPT_PATH=str(prompt), VENDOR_BUCKETS_PATH=str(mapping))
            with patch.dict(os.environ, values, clear=True), patch.object(sys, 'argv', ['doctor.py', '--check-imap']), patch.object(tahor_db, 'init_db'), patch.object(mailbox_settings, 'get_classify_mode', return_value='free'), patch.object(fetch_batch, 'connect', return_value=conn):
                self.assertEqual(doctor.main(), 0)
            conn.select.assert_called_once_with('"INBOX"', readonly=True)
            conn.logout.assert_called_once()

    def test_setup_is_private_free_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(config_dir=root / 'config', data_dir=None, systemd_dir=root / 'units', email='owner@example.com', imap_host='imap.example.com', smtp_host='smtp.example.com', base_url='http://localhost:8420', mode='free', non_interactive=True)
            with patch.dict(os.environ, {'FASTMAIL_APP_PASSWORD': 'a "$secret" with spaces', 'OPENROUTER_API_KEY': 'fake-key'}, clear=True):
                config = setup_tahor.configure(args)
                original = config.read_text()
                run.load_environment(config)
                self.assertEqual(os.environ['FASTMAIL_APP_PASSWORD'], 'a "$secret" with spaces')
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                settings = json.loads((root / 'config/state/settings.json').read_text())
                self.assertEqual(settings['classify_mode'], 'free')
                self.assertEqual(settings['rule_model'], 'none')
                self.assertEqual(settings['reply_model'], 'none')
                self.assertEqual(os.environ['TAHOR_CLASSIFY_FREE_ENABLED'], '0')
                self.assertEqual(os.environ['TAHOR_NOTIFY_HEALTH'], '0')
                self.assertEqual(os.environ['TAHOR_NOTIFY_DIGEST'], '0')
                notify = (root / 'units/tahor-notifications.service').read_text()
                self.assertIn('Type=oneshot', notify)
                self.assertIn(' notify ', notify)
                self.assertNotIn('Restart=always', notify)
                self.assertIn('OnCalendar=*:0/15', (root / 'units/tahor-notifications.timer').read_text())
                self.assertFalse((root / 'units/timers.target.wants').exists())
                (root / 'config/data/prompt.txt').write_text('My prompt')
                setup_tahor.configure(args)
                self.assertEqual(config.read_text(), original)
                self.assertEqual((root / 'config/data/prompt.txt').read_text(), 'My prompt')
                self.assertNotIn('fake-key', (root / 'units/tahor-backlog-worker.service').read_text())
                completed = subprocess.run([sys.executable, str(ROOT / 'run.py'), '--env', str(config), 'doctor'], capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertNotIn('fake-key', completed.stdout + completed.stderr)

    def test_environment_parser_does_not_execute_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.env'
            config.write_text('TOKEN="$(touch /tmp/never-run-tahor-test)"\n')
            with patch.dict(os.environ, {}, clear=True):
                run.load_environment(config)
                self.assertEqual(os.environ['TOKEN'], '$(touch /tmp/never-run-tahor-test)')


if __name__ == '__main__':
    unittest.main()
