import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ai_routing
import mailbox_settings
import tahor_db


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = {'p': {'model': 'paid'}, 'f': {'model': 'model:free'}}
        self.policy = 'paid'
        for target, attr, value in [(tahor_db, 'DB_PATH', Path(self.temp.name)/'db.sqlite')]:
            p = patch.object(target, attr, value); p.start(); self.addCleanup(p.stop)
        for name, kwargs in [('is_ai_enabled', {'return_value': True}),
                             ('get_ai_models', {'return_value': {'paid': 'p', 'free': 'f'}}),
                             ('get_ai_policy', {'side_effect': lambda task: self.policy})]:
            p = patch.object(mailbox_settings, name, **kwargs); p.start(); self.addCleanup(p.stop)
        self.now = 1000
        p = patch.object(ai_routing.time, 'time', side_effect=lambda: self.now)
        p.start(); self.addCleanup(p.stop)

    def run_task(self, operation, count=1):
        return ai_routing.run('reply', self.registry, operation, queue_size=count)

    def test_paid_only_never_uses_free_even_on_failure(self):
        self.policy = 'paid_only'; call = Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError): self.run_task(call)
        self.assertEqual([x.args[0] for x in call.call_args_list], ['p'])

    def test_free_never_spends_even_if_free_misconfigured(self):
        self.policy = 'free'; self.registry['f'] = {'model': 'not-free'}
        call = Mock()
        with self.assertRaises(ai_routing.RoutingUnavailable): self.run_task(call)
        call.assert_not_called()

    def test_paid_falls_back_and_probes_after_restart(self):
        call = Mock(side_effect=[TimeoutError(), 'free'])
        self.assertEqual(self.run_task(call), 'free')
        self.assertEqual([x.args[0] for x in call.call_args_list], ['p', 'f'])
        importlib.reload(ai_routing)
        self.now = 1200; call = Mock(return_value='free')
        self.run_task(call); call.assert_called_once_with('f')
        self.now = 1300; call = Mock(return_value='paid')
        self.run_task(call); call.assert_called_once_with('p')

    def test_auto_exact_four_hour_boundary_and_observed_latency(self):
        self.policy = 'auto'
        call = Mock(return_value='free')
        with patch.object(ai_routing.time, 'monotonic', side_effect=[0, 60]): self.run_task(call, 240)
        call.assert_called_once_with('f')
        call = Mock(return_value='paid'); self.run_task(call, 241)
        call.assert_called_once_with('p')

    def test_auto_free_failure_uses_paid_then_reprobes_free(self):
        self.policy = 'auto'; call = Mock(side_effect=[TimeoutError(), 'paid'])
        self.assertEqual(self.run_task(call), 'paid')
        self.assertEqual([x.args[0] for x in call.call_args_list], ['f', 'p'])
        self.now = 1299; call = Mock(return_value='paid'); self.run_task(call); call.assert_called_once_with('p')
        self.now = 1300; call = Mock(return_value='free'); self.run_task(call); call.assert_called_once_with('f')

    def test_quality_rejection_does_not_switch_or_create_early_alert(self):
        self.policy = 'auto'; call = Mock(side_effect=ValueError('private rejected content'))
        with self.assertRaises(ValueError): self.run_task(call)
        call.assert_called_once_with('f')
        self.assertEqual(ai_routing.persistent_problems(2799), [])
        self.assertEqual(ai_routing.persistent_problems(2800), ['ai_reply'])
        self.assertNotIn('private', ai_routing.state_path().read_text())
        self.assertEqual(ai_routing.state_path().stat().st_mode & 0o777, 0o600)

    def test_failure_duration_survives_retries_and_recovery_clears_alert(self):
        self.policy = 'free'; call = Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError): self.run_task(call)
        self.now = 2800
        with self.assertRaises(TimeoutError): self.run_task(call)
        self.assertEqual(ai_routing.persistent_problems(), ['ai_reply'])
        self.now = 3101; self.run_task(Mock(return_value='ok'))
        self.assertEqual(ai_routing.persistent_problems(), [])

    def test_successful_fallback_is_not_a_task_failure(self):
        self.run_task(Mock(side_effect=[TimeoutError(), 'ok']))
        self.assertEqual(ai_routing.persistent_problems(9999), [])

    def test_disabled_task_does_not_call_provider_or_alert(self):
        with patch.object(mailbox_settings, 'is_ai_enabled', return_value=False):
            call = Mock()
            with self.assertRaises(ai_routing.RoutingUnavailable): self.run_task(call)
            call.assert_not_called()
            self.assertEqual(ai_routing.persistent_problems(9999), [])

    def test_corrupt_state_recovers_without_breaking_free_policy(self):
        self.policy = 'free'
        ai_routing.state_path().write_text('invalid')
        call = Mock(return_value='ok')
        self.assertEqual(self.run_task(call), 'ok')
        call.assert_called_once_with('f')
        archived = list(ai_routing.state_path().parent.glob('*.corrupt-*'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(ai_routing.persistent_problems(), [])

    def test_owner_policy_change_prevents_new_paid_fallback(self):
        self.policy = 'auto'
        def fail(key):
            self.policy = 'free'
            raise TimeoutError()
        call = Mock(side_effect=fail)
        with self.assertRaises(ai_routing.RoutingUnavailable): self.run_task(call)
        call.assert_called_once_with('f')

    def test_other_success_cannot_hide_stuck_item_and_withdrawal_clears_it(self):
        self.policy = 'paid_only'
        with self.assertRaises(ValueError):
            ai_routing.run('reply', self.registry, Mock(side_effect=ValueError()), work_id='first')
        self.now = 2801
        ai_routing.run('reply', self.registry, Mock(return_value='ok'), work_id='second')
        self.assertEqual(ai_routing.persistent_problems(), ['ai_reply'])
        ai_routing.reconcile_pending('reply', ['second'])
        self.assertEqual(ai_routing.persistent_problems(), [])

    def test_rule_and_reply_cooldowns_are_independent(self):
        self.policy = 'paid_only'
        with self.assertRaises(TimeoutError): self.run_task(Mock(side_effect=TimeoutError()))
        call = Mock(return_value='proposal')
        ai_routing.run('rule', self.registry, call)
        call.assert_called_once_with('p')

    def test_batch_outcomes_preserve_partial_failure_and_clear_recovery(self):
        ai_routing.record_results('classification', [{'id':'bad','action':'error'}, {'id':'good','action':'keep'}])
        self.now = 2801
        ai_routing.record_results('classification', [{'id':'good','action':'keep'}])
        self.assertEqual(ai_routing.persistent_problems(), ['ai_classification'])
        ai_routing.record_results('classification', [{'id':'bad','action':'keep'}])
        self.assertEqual(ai_routing.persistent_problems(), [])
