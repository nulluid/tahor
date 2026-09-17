import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from data_changes import atomic_write
from provider_connector.auth import FastmailAuth, RateLimited, private_json, totp
from test_provider_auth import CREDENTIALS, SESSION
import test_provider_auth as fixtures


class TotpReuseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.credentials = self.root/'credentials.json'
        atomic_write(self.credentials, json.dumps(CREDENTIALS))
        self.now = 59.0
        self.waits = []
        self.auth = FastmailAuth(self.credentials, self.root/'state', clock=lambda: self.now, sleeper=self.sleep)

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay

    def test_login_then_settings_uses_next_window_and_persists_before_submission(self):
        bodies = []
        sequence = fixtures.AuthTests.sequence(self) + [
            (200, {'loginId': 'settings', 'methods': [{'type': 'password'}]}),
            (200, {'loginId': 'settings', 'methods': [{'type': 'totp'}]}),
            (201, {'sudoUntil': '1970-01-01T01:00:00Z'})]
        def request(method, url, body, **kwargs):
            bodies.append(body)
            if body['type'] == 'totp':
                saved = private_json(self.root/'state/session.json')['auth_state']['totp_step']
                self.assertEqual(saved, int(self.now // 30))
            return sequence.pop(0)
        with patch.object(self.auth, 'request', side_effect=request):
            self.auth.ensure_session()
            self.auth.ensure_settings_auth()
        values = [b['value'] for b in bodies if b['type'] == 'totp']
        self.assertEqual(values, [totp(CREDENTIALS['totp_seed'], 59), totp(CREDENTIALS['totp_seed'], 61)])
        self.assertEqual(self.waits, [2.0])
        self.assertNotEqual(*values)

    def test_restart_and_login_state_rewrite_preserve_reserved_window(self):
        self.auth.next_totp(CREDENTIALS['totp_seed'])
        restarted = FastmailAuth(self.credentials, self.root/'state', clock=lambda: self.now, sleeper=self.sleep)
        with patch.object(restarted, 'request', side_effect=fixtures.AuthTests.sequence(self)):
            restarted.ensure_session()
        self.assertEqual(self.waits, [2.0])
        self.assertEqual(restarted.auth_state['totp_step'], 2)
        self.assertEqual(restarted.auth_state['user_id'], SESSION['userId'])

    def test_clock_backwards_refuses_without_wait_or_reset(self):
        self.auth.auth_state['totp_step'] = 3
        with self.assertRaises(RateLimited):
            self.auth.next_totp(CREDENTIALS['totp_seed'])
        self.assertEqual(self.waits, [])
        self.assertEqual(self.auth.auth_state['totp_step'], 3)

    def test_stalled_clock_waits_once_at_most31_seconds_then_refuses(self):
        self.now = 30.0
        self.auth.auth_state['totp_step'] = 1
        self.auth.sleeper = self.waits.append
        with self.assertRaises(RateLimited):
            self.auth.next_totp(CREDENTIALS['totp_seed'])
        self.assertEqual(self.waits, [31.0])
        self.assertEqual(self.auth.auth_state['totp_step'], 1)
