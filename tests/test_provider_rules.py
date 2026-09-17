import copy
from pathlib import Path
import tempfile
import unittest

from provider_connector.auth import ProtocolError
from provider_connector.rules import RuleManager, Conflict, rule_for, domains

INSTALLATION = 'a'*32


class FakeManager(RuleManager):
    def __init__(self, state):
        super().__init__(None, state, INSTALLATION)
        self.records = {'personal': {'id': 'personal', 'name': 'Personal rule', 'redirectTo': ['me@example.com']}}
        self.calls = []
        self.fail_after_create = False

    def call(self, method, args):
        self.calls.append((method, copy.deepcopy(args)))
        if method == 'Rule/get':
            return {'list': copy.deepcopy(list(self.records.values()))}
        if 'create' in args:
            identifier = str(len(self.calls))
            self.records[identifier] = dict(args['create']['new'], id=identifier)
            if self.fail_after_create:
                raise TimeoutError()
            return {'created': {'new': {'id': identifier}}}
        for identifier in args['destroy']:
            del self.records[identifier]
        return {'destroyed': args['destroy']}


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = FakeManager(self.root)

    def test_create_idempotent_then_unblock_preserves_other_rules(self):
        original = copy.deepcopy(self.manager.records['personal'])
        self.assertEqual(self.manager.sync(['example.com']), 1)
        self.assertEqual(self.manager.sync(['example.com']), 1)
        self.assertEqual(len([c for c in self.manager.calls if 'create' in c[1]]), 1)
        self.assertEqual(self.manager.sync([]), 0)
        self.assertEqual(self.manager.records, {'personal': original})

    def test_manually_changed_owned_rule_is_not_overwritten_or_deleted(self):
        self.manager.sync(['example.com'])
        key = self.manager.journal['owned']['example.com']
        self.manager.records[key]['redirectTo'] = ['owner@example.com']
        before = copy.deepcopy(self.manager.records)
        with self.assertRaises(Conflict):
            self.manager.sync([])
        self.assertEqual(before, self.manager.records)

    def test_uncertain_create_recovers_without_duplicate(self):
        self.manager.fail_after_create = True
        with self.assertRaises(TimeoutError):
            self.manager.sync(['example.com'])
        self.assertEqual(self.manager.journal['pending'], 'example.com')
        self.manager.fail_after_create = False
        self.assertEqual(self.manager.sync(['example.com']), 1)
        self.assertEqual(len(self.manager.records), 2)

    def test_uncertain_create_without_visible_result_requires_review(self):
        self.manager.journal['pending'] = 'example.com'
        with self.assertRaises(Conflict):
            self.manager.sync(['example.com'])
        self.assertFalse(any('create' in c[1] for c in self.manager.calls))

    def test_deleted_owned_rule_is_not_silently_recreated(self):
        self.manager.sync(['example.com'])
        del self.manager.records[self.manager.journal['owned']['example.com']]
        with self.assertRaises(Conflict):
            self.manager.sync(['example.com'])

    def test_domain_injection_and_arbitrary_actions_rejected(self):
        for values in [['example.com"; redirect "x'], ['*.com'], ['com'], ['-bad.com'], ['a..com'], ['UPPER.com'], ['a.com', 'a.com'], ['a.com']*251, 'example.com']:
            with self.assertRaises(ProtocolError):
                domains(values)
        rule = rule_for('example.com', INSTALLATION)
        self.assertIsNone(rule['redirectTo'])
        self.assertIsNone(rule['fileIn'])
        self.assertEqual(rule['conditions'][0]['lookFor'], 'address :domain :is "from" "example.com"')


class SettingsMutationTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = FakeManager(Path(self.temp.name))
        self.manager.auth = Mock()

    def test_reauthentication_completes_before_reading_state_or_creating_rules(self):
        observed = []
        self.manager.auth.ensure_settings_auth.side_effect = lambda: observed.append(list(self.manager.calls))
        self.manager.sync(['example.com'])
        self.assertEqual(observed, [[]])
        self.manager.calls.clear()
        self.manager.auth.ensure_settings_auth.reset_mock()
        self.manager.sync(['example.com'])
        self.manager.auth.ensure_settings_auth.assert_not_called()

    def test_rejected_settings_auth_leaves_provider_and_journal_untouched(self):
        from provider_connector.auth import AuthenticationRequired
        self.manager.auth.ensure_settings_auth.side_effect = AuthenticationRequired()
        with self.assertRaises(AuthenticationRequired):
            self.manager.sync(['example.com'])
        self.assertEqual(self.manager.calls, [])
        self.assertIsNone(self.manager.journal['pending'])
        self.assertEqual(set(self.manager.records), {'personal'})

    def test_permission_failure_after_create_intent_is_not_blindly_replayed(self):
        from provider_connector.auth import AuthenticationRequired
        from unittest.mock import patch
        original = self.manager.call
        def reject_create(method, args):
            if 'create' in args:
                raise AuthenticationRequired()
            return original(method, args)
        with patch.object(self.manager, 'call', side_effect=reject_create):
            with self.assertRaises(AuthenticationRequired):
                self.manager.sync(['example.com'])
        self.assertEqual(self.manager.journal['pending'], 'example.com')
        with self.assertRaises(Conflict):
            self.manager.sync(['example.com'])
        self.assertEqual(set(self.manager.records), {'personal'})

    def test_changed_provider_rule_during_reauthentication_is_preserved(self):
        self.manager.sync(['example.com'])
        key = self.manager.journal['owned']['example.com']
        def user_edit():
            self.manager.records[key]['discard'] = False
        self.manager.auth.ensure_settings_auth.side_effect = user_edit
        with self.assertRaises(Conflict):
            self.manager.sync([])
        self.assertIn(key, self.manager.records)
        self.assertFalse(self.manager.records[key]['discard'])
