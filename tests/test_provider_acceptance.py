import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from provider_connector.acceptance import lifecycle


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.auth = Mock(credentials_path=self.root/'credential', state_dir=self.root)
        self.auth.auth_state = {'user_id': 'bound', 'next_login': 1}
        self.auth.session = {'accessToken': 'private'}
        self.remote = {'custom': {'name': 'Private custom rule', 'id': 'custom'}}
        self.calls = []
        owner = self
        class Manager:
            def __init__(self, auth, path, installation):
                self.auth = auth
                self.journal = {'owned': {}, 'pending': None}
            def get(self):
                return dict(owner.remote)
            def sync(self, domains):
                owner.calls.append(list(domains))
                for key in list(owner.remote):
                    if key != 'custom': del owner.remote[key]
                self.journal['owned'] = {}
                for domain in domains:
                    if not domain.endswith('.invalid'): raise AssertionError('Unsafe test target')
                    owner.remote[domain] = {'name': domain}
                    self.journal['owned'][domain] = domain
                return len(domains)
        self.manager = Manager

    def test_restart_change_cleanup_and_unrelated_rule_hashes(self):
        with patch('provider_connector.acceptance.RuleManager', self.manager), patch(
                'provider_connector.acceptance.FastmailAuth', return_value=self.auth):
            result = lifecycle(self.auth, self.root)
        self.assertTrue(result['success'])
        self.assertTrue(result['restart_verified'])
        self.assertTrue(result['managed_change_verified'])
        self.assertEqual(self.calls[0], self.calls[1])
        self.assertNotEqual(self.calls[1], self.calls[2])
        self.assertEqual(self.calls[-1], [])
        self.assertEqual(set(self.remote), {'custom'})
        self.assertFalse(result['fresh_login_verified'])
        self.auth.ensure_session.assert_called_once()

    def test_failure_always_attempts_cleanup_and_preserves_private_evidence(self):
        self.auth.ensure_settings_auth.side_effect = RuntimeError('PRIVATE BODY')
        with patch('provider_connector.acceptance.RuleManager', self.manager):
            result = lifecycle(self.auth, self.root)
        self.assertFalse(result['success'])
        self.assertEqual(self.calls, [[]])
        evidence = next(self.root.glob('acceptance-*/result.json')).read_text()
        self.assertNotIn('PRIVATE', evidence)
        self.assertIn('RuntimeError', evidence)

    def test_fresh_login_never_bypasses_guard(self):
        self.auth.auth_state['blocked'] = True
        with patch('provider_connector.acceptance.RuleManager', self.manager):
            result = lifecycle(self.auth, self.root, fresh_login=True)
        self.assertFalse(result['success'])
        self.auth.ensure_session.assert_not_called()
        self.auth.http.cookies.clear.assert_not_called()
        self.assertTrue(self.auth.auth_state['blocked'])
        self.assertEqual(self.calls, [[]])

    def test_explicit_fresh_login_preserves_binding_and_attempt_guards(self):
        self.auth.auth_state['settings_attempt'] = {'revision': 'existing', 'next_attempt': 1}
        original = dict(self.auth.auth_state)
        with patch('provider_connector.acceptance.RuleManager', self.manager), patch(
                'provider_connector.acceptance.FastmailAuth', return_value=self.auth):
            result = lifecycle(self.auth, self.root, fresh_login=True)
        self.assertTrue(result['success'])
        self.assertTrue(result['fresh_login_verified'])
        self.assertEqual(self.auth.auth_state, original)
        self.auth.http.cookies.clear.assert_called_once()
