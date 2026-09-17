"""Classification routing policies preserve results and recover without tier leaks."""
import os
import unittest
from unittest.mock import patch

import backlog_worker as worker


def results(records, action='keep', status=None):
    return [dict(id=r['id'], action=action, **({'http_status': status} if status else {})) for r in records]


class ClassifierPolicyTests(unittest.TestCase):
    def setUp(self):
        for target in ('_load_cooldowns', '_policy_unchanged'):
            patcher = patch.object(worker, target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(worker.ai_routing, 'locked_state')
        state = patcher.start()
        state.return_value.__enter__.return_value = {}
        self.addCleanup(patcher.stop)

        self.records = [{'id': str(i)} for i in range(3)]
        self.patches = [patch.dict(os.environ, {'TAHOR_CLASSIFY_FREE_ENABLED': '1'}),
                        patch.object(worker, '_paid_retry_at', 0),
                        patch.object(worker, '_free_retry_at', 0),
                        patch.object(worker, 'log'),
                        patch.object(worker, 'get_backlog_estimate', return_value=3),
                        patch.object(worker.mailbox_settings, 'recent_free_rate', return_value=1),
                        patch.object(worker.mailbox_settings, 'decide_backend_split', return_value=(3, 0))]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = self.start(patch.object(worker.time, 'monotonic', return_value=100))
        self.free = self.start(patch.object(worker, '_classify_free_and_time', side_effect=results))
        self.paid = self.start(patch.object(worker, 'classify_with_backend', side_effect=lambda records, backend: results(records)))

    def start(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_paid_only_credit_failure_cooldown_and_recovery_never_call_free(self):
        self.paid.side_effect = lambda records, backend: results(records, 'error', 402)
        free, paid = worker.classify_batch(self.records, 'paid_only')
        self.assertEqual(free, [])
        self.assertEqual(paid, results(self.records, 'error', 402))
        worker.classify_batch(self.records, 'paid_only')
        self.assertEqual(self.paid.call_count, 1)
        self.clock.return_value = 401
        self.paid.side_effect = lambda records, backend: results(records)
        self.assertEqual(worker.classify_batch(self.records, 'paid_only'), ([], results(self.records)))
        self.assertEqual(self.paid.call_args_list[1].args[0], self.records[:1])
        self.assertEqual(worker._paid_retry_at, 0)
        self.free.assert_not_called()

    def test_auto_retries_only_free_failures_then_probes_free_after_cooldown(self):
        self.free.side_effect = lambda records: results(records[:1]) + results(records[1:], 'error', 429)
        free, paid = worker.classify_batch(self.records, 'auto')
        self.assertEqual(free, results(self.records[:1]))
        self.assertEqual(paid, results(self.records[1:]))
        self.paid.assert_called_once_with(self.records[1:], 'openrouter-paid')
        self.assertEqual(worker._free_retry_at, 400)
        worker.classify_batch(self.records, 'auto')
        self.assertEqual(self.free.call_count, 1)
        self.clock.return_value = 401
        self.free.side_effect = results
        self.assertEqual(worker.classify_batch(self.records, 'auto'), (results(self.records), []))
        self.assertEqual(self.free.call_args_list[1].args[0], self.records[:1])
        self.assertEqual(worker._free_retry_at, 0)
        self.assertEqual(self.paid.call_count, 2)

    def test_failed_free_probe_reschedules_without_batch_flood(self):
        worker._free_retry_at = 99
        self.free.side_effect = lambda records: results(records, 'error', 429)
        self.assertEqual(worker.classify_batch(self.records, 'auto'), ([], results(self.records)))
        self.free.assert_called_once_with(self.records[:1])
        self.assertEqual(worker._free_retry_at, 400)

    def test_both_routes_fail_without_ping_pong_or_losing_records(self):
        self.free.side_effect = lambda records: results(records, 'error', 429)
        self.paid.side_effect = lambda records, backend: results(records, 'error', 402)
        self.assertEqual(worker.classify_batch(self.records, 'auto'), ([], results(self.records, 'error', 402)))
        worker.classify_batch(self.records, 'auto')
        self.assertEqual(self.free.call_count, 1)
        self.assertEqual(self.paid.call_count, 1)

    def test_free_only_ignores_both_circuits_and_never_charges(self):
        worker._paid_retry_at = 99
        worker._free_retry_at = 400
        self.free.side_effect = lambda records: results(records, 'error', 429)
        self.assertEqual(worker.classify_batch(self.records, 'free'), (results(self.records, 'error', 429), []))
        self.paid.assert_not_called()

    def test_invalid_mode_fails_before_requests(self):
        with self.assertRaises(ValueError):
            worker.classify_batch(self.records, 'unknown')
        self.free.assert_not_called()
        self.paid.assert_not_called()
