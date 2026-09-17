import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from data_changes import atomic_write
from provider_connector.auth import (FastmailAuth, AuthenticationRequired, ProtocolError,
                                    CredentialError, RateLimited, endpoint, totp, private_json, MAIL)

SEED = base64.b32encode(b'12345678901234567890').decode()
CREDENTIALS = {'username': 'test@example.com', 'password': 'private-test-password', 'totp_seed': SEED}
SESSION = {'username': 'test@example.com', 'userId': 'test-user', 'accessToken': 'private-test-token',
           'apiUrl': 'https://phl.api.fastmail.com/jmap/api/', 'primaryAccounts': {MAIL: 'test-account'}}


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.credentials = self.root/'credentials.json'
        atomic_write(self.credentials, json.dumps(CREDENTIALS))
        self.auth = FastmailAuth(self.credentials, self.root/'state', clock=lambda: 59)

    def test_rfc6238_vectors_six_digits(self):
        for timestamp, expected in [(59, '287082'), (1111111109, '081804'), (1111111111, '050471'), (1234567890, '005924'), (2000000000, '279037'), (20000000000, '353130')]:
            self.assertEqual(totp(SEED, timestamp), expected)

    def test_manual_seed_grouping_and_128_bit_issuer_keys(self):
        seed = base64.b32encode(b'1234567890123456').decode().rstrip('=')
        grouped = '-'.join(seed.lower()[i:i+4] for i in range(0, len(seed), 4))
        self.assertEqual(totp('  ' + grouped + '\t\n', 59), totp(seed, 59))
        self.assertEqual(len(totp(seed, 59)), 6)
        for invalid in ('123456', 'otpauth://totp/example?secret=' + seed,
                        base64.b32encode(b'too-short').decode(), seed + '!'):
            with self.assertRaises(CredentialError):
                totp(invalid, 59)

    def test_endpoint_rejects_secret_exfiltration_and_redirects(self):
        for url in ('http://api.fastmail.com/auth/login', 'https://api.fastmail.com.evil.test/auth/login',
                    'https://api.fastmail.com@evil.test/auth/login', 'https://api.fastmail.com/auth/login?next=evil',
                    'https://api.fastmail.com/auth/sudo', 'https://localhost/auth/login'):
            with self.assertRaises(ProtocolError):
                endpoint(url, '/auth/login')

    def test_private_file_rejects_symlink_and_readable_credentials(self):
        self.credentials.chmod(0o644)
        with self.assertRaises(CredentialError):
            private_json(self.credentials)
        link = self.root/'link'
        link.symlink_to(self.credentials)
        with self.assertRaises(OSError):
            private_json(link)

    def sequence(self):
        return [(200, {'loginId': 'one', 'methods': [{'type': 'username'}], 'nextUrl': 'https://phl.api.fastmail.com/auth/login'}),
                (200, {'loginId': 'two', 'methods': [{'type': 'password'}]}),
                (200, {'loginId': 'three', 'methods': [{'type': 'totp'}]}), (201, SESSION)]

    def test_fresh_login_password_then_totp_and_persistence(self):
        with patch.object(self.auth, 'request', side_effect=self.sequence()) as request:
            self.assertEqual(self.auth.ensure_session()['userId'], 'test-user')
        bodies = [call.args[2] for call in request.call_args_list]
        self.assertEqual([b['type'] for b in bodies], ['start', 'username', 'password', 'totp'])
        self.assertEqual(bodies[-1]['value'], '287082')
        self.assertFalse(bodies[-1]['remember'])
        self.assertEqual((self.root/'state/session.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(FastmailAuth(self.credentials, self.root/'state').session, SESSION)

    def test_bad_password_blocks_across_restart(self):
        with patch.object(self.auth, 'request', side_effect=AuthenticationRequired()):
            with self.assertRaises(AuthenticationRequired):
                self.auth.ensure_session()
        restart = FastmailAuth(self.credentials, self.root/'state', clock=lambda: 99999)
        with patch.object(restart, 'request') as request:
            with self.assertRaises(AuthenticationRequired):
                restart.ensure_session()
            request.assert_not_called()

    def test_network_failure_reserves_persistent_login_cooldown(self):
        with patch.object(self.auth, 'request', side_effect=RateLimited()):
            with self.assertRaises(RateLimited):
                self.auth.ensure_session()
        restart = FastmailAuth(self.credentials, self.root/'state', clock=lambda: 100)
        with patch.object(restart, 'request') as request:
            with self.assertRaises(RateLimited):
                restart.ensure_session()
            request.assert_not_called()

    def test_expired_session_falls_back_to_full_login(self):
        self.auth.session = SESSION
        with patch.object(self.auth, 'request', side_effect=[AuthenticationRequired(), *self.sequence()]):
            self.assertEqual(self.auth.ensure_session()['username'], CREDENTIALS['username'])

    def test_live_session_does_not_send_password(self):
        self.auth.session = SESSION
        with patch.object(self.auth, 'request', return_value=(200, [SESSION])) as request:
            self.auth.ensure_session()
            request.assert_called_once_with('GET', 'https://phl.api.fastmail.com/auth/sessions')

    def test_wrong_account_and_unsupported_challenges_stop(self):
        for response in [(201, dict(SESSION, username='other@example.com')),
                         (200, {'loginId': 'one', 'methods': [{'type': 'sms'}]}),
                         (200, {'loginId': 'one', 'methods': [{'type': 'username'}], 'nextUrl': 'https://evil.test/auth/login'})]:
            auth = FastmailAuth(self.credentials, self.root/'state', clock=lambda: 99999)
            auth.auth_state = {}
            with patch.object(auth, 'request', return_value=response):
                with self.assertRaises((AuthenticationRequired, ProtocolError)):
                    auth.ensure_session()


class SettingsAuthenticationTests(unittest.TestCase):
    def setUp(self):
        AuthTests.setUp(self)
        self.auth.session = dict(SESSION)
        self.expiry = '1970-01-01T01:00:00Z'

    def sequence(self):
        return [(200, {'loginId': 'step-one', 'methods': [{'type': 'password'}]}),
                (200, {'loginId': 'step-two', 'methods': [{'type': 'totp'}]}),
                (201, {'sudoUntil': self.expiry})]

    # Only inherit fixture helpers, not the separate full-login test cases.
    def test_settings_password_and_totp_use_current_session_and_provider_expiry(self):
        with patch.object(self.auth, 'request', side_effect=self.sequence()) as request:
            self.auth.ensure_settings_auth()
        self.assertEqual([c.args[2]['type'] for c in request.call_args_list], ['start', 'password', 'totp'])
        for call in request.call_args_list:
            self.assertEqual(call.args[1], 'https://phl.api.fastmail.com/auth/sudo')
            self.assertEqual(call.kwargs['token'], SESSION['accessToken'])
        self.assertEqual(request.call_args_list[1].args[2]['value'], CREDENTIALS['password'])
        self.assertEqual(request.call_args_list[2].args[2]['value'], '287082')
        self.assertEqual(self.auth.auth_state['settings_until'], 3600)
        restart = FastmailAuth(self.credentials, self.root/'state', clock=lambda: 100)
        with patch.object(restart, 'request') as request:
            restart.ensure_settings_auth()
            request.assert_not_called()

    def test_password_only_and_already_authorized_responses(self):
        for sequence in ([self.sequence()[0], self.sequence()[-1]], [self.sequence()[-1]]):
            self.auth.auth_state = {}
            with patch.object(self.auth, 'request', side_effect=sequence):
                self.auth.ensure_settings_auth()
            self.assertEqual(self.auth.auth_state['settings_until'], 3600)

    def test_expiry_requires_new_settings_auth(self):
        self.auth.auth_state['settings_until'] = 100
        with patch.object(self.auth, 'request', side_effect=self.sequence()) as request:
            self.auth.ensure_settings_auth()
        self.assertEqual(request.call_count, 3)

    def test_changed_access_token_invalidates_settings_authorization(self):
        self.auth.auth_state['settings_until'] = 3600
        self.auth.accept(dict(SESSION, accessToken='rotated-test-token'), CREDENTIALS['username'])
        self.assertNotIn('settings_until', self.auth.auth_state)

    def test_rejected_password_or_totp_blocks_after_restart(self):
        for preceding in ([], self.sequence()[:1], self.sequence()[:2]):
            self.auth.auth_state = {}
            with patch.object(self.auth, 'request', side_effect=[*preceding, AuthenticationRequired()]):
                with self.assertRaises(AuthenticationRequired):
                    self.auth.ensure_settings_auth()
            restart = FastmailAuth(self.credentials, self.root/'state', clock=lambda: 99999)
            with patch.object(restart, 'request') as request:
                with self.assertRaises(AuthenticationRequired):
                    restart.ensure_settings_auth()
                request.assert_not_called()

    def test_network_failure_has_restart_safe_cooldown_and_eventual_retry(self):
        with patch.object(self.auth, 'request', side_effect=RateLimited()):
            with self.assertRaises(RateLimited):
                self.auth.ensure_settings_auth()
        restart = FastmailAuth(self.credentials, self.root/'state', clock=lambda: 100)
        with patch.object(restart, 'request') as request:
            with self.assertRaises(RateLimited):
                restart.ensure_settings_auth()
            request.assert_not_called()
        restart.clock = lambda: 1000
        with patch.object(restart, 'request', side_effect=self.sequence()):
            restart.ensure_settings_auth()

    def test_protocol_changes_stop_without_leaking_secrets(self):
        for response in [(200, {'loginId': 'one', 'methods': [{'type': 'sms'}]}),
                         (200, {'loginId': 'one', 'methods': None}),
                         (200, {'loginId': 'one', 'methods': [], 'nextUrl': 'https://evil.test/auth/sudo'}),
                         (201, {'sudoUntil': '2099-01-01T00:00:00'}),
                         (201, {'sudoUntil': '1970-01-01T00:01:00Z'}),
                         (201, {})]:
            self.auth.auth_state = {}
            with patch.object(self.auth, 'request', return_value=response):
                with self.assertRaises((AuthenticationRequired, ProtocolError)) as caught:
                    self.auth.ensure_settings_auth()
            self.assertEqual(str(caught.exception), '')
            self.assertTrue(self.auth.auth_state['settings_attempt']['blocked'])

    def test_new_login_does_not_reset_rejected_settings_credentials(self):
        with patch.object(self.auth, 'request', side_effect=AuthenticationRequired()):
            with self.assertRaises(AuthenticationRequired):
                self.auth.ensure_settings_auth()
        self.auth.session = None
        with patch.object(self.auth, 'request', side_effect=AuthTests.sequence(self)):
            self.auth.ensure_session()
        with patch.object(self.auth, 'request') as request:
            with self.assertRaises(AuthenticationRequired):
                self.auth.ensure_settings_auth()
            request.assert_not_called()
