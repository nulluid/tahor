import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from provider_connector.auth import AuthenticationRequired, ProtocolError
from provider_connector.service import check_auth


class ProviderCheckTests(unittest.TestCase):
    def auth(self):
        auth = Mock()
        auth.session = {'accessToken': 'PRIVATE TOKEN'}
        auth.auth_state = {'blocked': True, 'next_login': 9999999999, 'user_id': 'PRIVATE USER',
                           'settings_attempt': {'blocked': True, 'next_attempt': 9999999999}, 'settings_until': 123}
        auth.diagnostic = {}
        return auth

    def check(self, auth, **kwargs):
        output = io.StringIO()
        with patch('provider_connector.service.FastmailAuth', return_value=auth), contextlib.redirect_stdout(output):
            result = check_auth({'credentials': 'private', 'state': 'private'}, **kwargs)
        return result, output.getvalue()

    def test_normal_fresh_check_preserves_persistent_guards(self):
        auth = self.auth()
        previous = dict(auth.auth_state)
        auth.ensure_session.side_effect = AuthenticationRequired('PRIVATE PASSWORD')
        success, output = self.check(auth)
        self.assertFalse(success)
        self.assertEqual(auth.auth_state, previous)
        auth.ensure_settings_auth.assert_not_called()
        self.assertNotIn('PRIVATE', output)
        self.assertIn('authentication_required', output)

    def test_settings_check_never_logs_in_or_resets_guards(self):
        auth = self.auth()
        previous = dict(auth.auth_state)
        session = auth.session
        auth.ensure_settings_auth.side_effect = AuthenticationRequired()
        self.assertFalse(self.check(auth, settings_only=True)[0])
        self.assertEqual(auth.auth_state, previous)
        self.assertIs(auth.session, session)
        auth.ensure_session.assert_not_called()
        auth.http.cookies.clear.assert_not_called()

    def test_explicit_settings_retry_preserves_session_identity_and_login_guard(self):
        auth = self.auth()
        session = auth.session
        self.assertTrue(self.check(auth, settings_only=True, retry_settings=True)[0])
        self.assertEqual(auth.auth_state, {'blocked': True, 'next_login': 9999999999, 'user_id': 'PRIVATE USER'})
        self.assertIs(auth.session, session)
        auth.ensure_session.assert_not_called()
        auth.save.assert_called_once()
        auth.ensure_settings_auth.assert_called_once()

    def test_diagnostic_output_allowlists_provider_shape_without_values(self):
        auth = self.auth()
        auth.diagnostic = {'phase': 'settings_start', 'http_status': 200, 'response_object': True,
                           'login_id_present': False, 'expiry_present': False,
                           'methods': ['totp', 'PRIVATE CHALLENGE'], 'secret': 'PRIVATE TOKEN'}
        auth.ensure_settings_auth.side_effect = ProtocolError('PRIVATE RESPONSE')
        success, output = self.check(auth, settings_only=True)
        self.assertFalse(success)
        self.assertNotIn('PRIVATE', output)
        self.assertIn('settings_start', output)
        self.assertIn('protocol_changed', output)
        self.assertIn('totp', output)

    def test_helper_rejects_retry_without_settings_only_check(self):
        with patch('provider_connector.service.FastmailAuth') as auth:
            with self.assertRaises(ValueError):
                check_auth({}, retry_settings=True)
            auth.assert_not_called()
