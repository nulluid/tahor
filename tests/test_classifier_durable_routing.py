"""Worker restarts and owner policy changes cannot silently reopen a tier."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backlog_worker as worker


def results(records, action='keep', status=None):
    return [dict(id=record['id'], action=action,
                 **({'http_status': status} if status else {})) for record in records]


class DurableClassifierRoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / 'routing.json'
        self.records = [{'id': str(i)} for i in range(3)]
        self.mode = 'paid_only'
        self.start(patch.object(worker.ai_routing, 'state_path', return_value=self.state))
        self.start(patch.object(worker, '_paid_retry_at', 0))
        self.start(patch.object(worker, '_free_retry_at', 0))
        self.wall = self.start(patch.object(worker.time, 'time', return_value=1000))
        self.mono = self.start(patch.object(worker.time, 'monotonic', return_value=100))
        self.start(patch.object(worker.mailbox_settings, 'get_classify_mode', side_effect=lambda: self.mode))
        self.start(patch.object(worker.classify, 'free_classification_enabled', return_value=True))
        self.start(patch.object(worker, 'get_backlog_estimate', return_value=3))
        self.start(patch.object(worker.mailbox_settings, 'recent_free_rate', return_value=1))
        self.start(patch.object(worker.mailbox_settings, 'decide_backend_split', return_value=(3, 0)))
        self.start(patch.object(worker, 'log'))
        self.paid = self.start(patch.object(worker, 'classify_with_backend', side_effect=lambda records, backend: results(records)))
        self.free = self.start(patch.object(worker, '_classify_free_and_time', side_effect=results))

    def start(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def restart_clock(self, elapsed):
        # Discard process memory and simulate a new monotonic epoch.
        worker._paid_retry_at = worker._free_retry_at = 0
        self.wall.return_value = 1000 + elapsed
        self.mono.return_value = 10

    def test_paid_cooldown_survives_restart_and_recovers_with_one_probe(self):
        self.paid.side_effect = lambda records, backend: results(records, 'error', 402)
        worker.classify_batch(self.records, self.mode)
        self.assertEqual(json.loads(self.state.read_text())['classifier_circuits'], {'paid': 1300})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.restart_clock(100)
        worker.classify_batch(self.records, self.mode)
        self.assertEqual(self.paid.call_count, 1)
        self.restart_clock(301)
        self.paid.side_effect = lambda records, backend: results(records)
        self.assertEqual(worker.classify_batch(self.records, self.mode), ([], results(self.records)))
        self.assertEqual(self.paid.call_args_list[1].args[0], self.records[:1])
        self.assertEqual(json.loads(self.state.read_text())['classifier_circuits'], {})
        self.free.assert_not_called()

    def test_auto_free_cooldown_survives_restart_and_uses_one_free_probe(self):
        self.mode = 'auto'
        self.free.side_effect = lambda records: results(records, 'error', 429)
        worker.classify_batch(self.records, self.mode)
        self.restart_clock(100)
        worker.classify_batch(self.records, self.mode)
        self.assertEqual(self.free.call_count, 1)
        self.restart_clock(301)
        self.free.side_effect = results
        self.assertEqual(worker.classify_batch(self.records, self.mode), (results(self.records), []))
        self.assertEqual(self.free.call_args_list[1].args[0], self.records[:1])
        self.assertEqual(self.paid.call_count, 2)

    def test_switch_to_always_free_during_auto_does_not_buy_fallback(self):
        self.mode = 'auto'
        def fail(records):
            self.mode = 'free'
            return results(records[:1]) + results(records[1:], 'error', 429)
        self.free.side_effect = fail
        free, paid = worker.classify_batch(self.records, 'auto')
        self.assertEqual(free, results(self.records[:1]) + results(self.records[1:], 'error', 429))
        self.assertEqual(paid, [])
        self.paid.assert_not_called()
        self.assertNotIn('classifier_circuits', json.loads(self.state.read_text()))

    def test_switch_to_always_paid_during_paid_call_does_not_use_free(self):
        self.mode = 'paid'
        def fail(records, backend):
            self.mode = 'paid_only'
            return results(records, 'error', 402)
        self.paid.side_effect = fail
        self.assertEqual(worker.classify_batch(self.records, 'paid'), ([], results(self.records, 'error', 402)))
        self.free.assert_not_called()

    def test_switch_during_successful_paid_probe_preserves_result_and_queues_rest(self):
        worker._set_cooldown('paid', 300)
        self.restart_clock(301)
        def success(records, backend):
            self.mode = 'free'
            return results(records)
        self.paid.side_effect = success
        free, paid = worker.classify_batch(self.records, 'paid_only')
        self.assertEqual(free, [])
        self.assertEqual(paid[0], results(self.records[:1])[0])
        self.assertTrue(all(item['action'] == 'error' for item in paid[1:]))
        self.paid.assert_called_once_with(self.records[:1], 'openrouter-paid')
        self.free.assert_not_called()

    def test_stale_batch_policy_never_calls_provider(self):
        self.mode = 'free'
        free, paid = worker.classify_batch(self.records, 'paid_only')
        self.assertEqual(len(free + paid), len(self.records))
        self.assertTrue(all(item['action'] == 'error' for item in free + paid))
        self.free.assert_not_called()
        self.paid.assert_not_called()

    def test_clock_correction_cannot_create_permanent_cooldown(self):
        self.state.write_text(json.dumps({'classifier_circuits': {'paid': 999999}}))
        worker._load_cooldowns()
        self.assertEqual(worker._paid_retry_at, 400)
        self.assertEqual(json.loads(self.state.read_text())['classifier_circuits']['paid'], 1300)
        self.restart_clock(301)
        worker.classify_batch(self.records, self.mode)
        self.assertEqual(self.paid.call_args_list[0].args[0], self.records[:1])

    def test_free_policy_never_uses_paid_after_restart(self):
        worker._set_cooldown('paid', 300)
        self.restart_clock(301)
        self.mode = 'free'
        self.assertEqual(worker.classify_batch(self.records, self.mode), (results(self.records), []))
        self.paid.assert_not_called()

    def test_failed_persistence_does_not_continue_to_free_fallback(self):
        self.mode = 'paid'
        self.paid.side_effect = lambda records, backend: results(records, 'error', 402)
        with patch.object(worker.ai_routing, 'atomic_write', side_effect=[None, OSError('disk full')]):
            with self.assertRaises(OSError):
                worker.classify_batch(self.records, self.mode)
        self.free.assert_not_called()
