import io
import json
import os
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import classify


class PaidPacingTests(unittest.TestCase):
    URL = 'https://openrouter.ai/api/v1/chat/completions'
    MODEL = 'google/gemini-3.8-flash'
    PROVIDER = {'only': ['google-vertex/global']}

    def setUp(self):
        self.now = 100.0
        self.sleeps = []
        for patcher in (patch.dict(os.environ, {'TAHOR_PAID_REQUEST_INTERVAL_SECONDS': '3'}),
                        patch.object(classify, '_next_request_start', {}),
                        patch.object(classify, '_request_pacing_lock', threading.Lock()),
                        patch.object(classify.time, 'monotonic', side_effect=lambda: self.now),
                        patch.object(classify.time, 'sleep', side_effect=self.sleep)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def test_threads_share_start_reservations_without_real_waiting(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: classify.wait_for_model_request(
                self.URL, self.MODEL, self.PROVIDER), range(8)))
        self.assertEqual(self.sleeps, [3.0] * 7)
        self.assertEqual(self.now, 121.0)
        self.assertEqual(list(classify._next_request_start.values()), [124.0])

    def test_idle_period_has_no_catchup_burst_and_other_models_are_unpaced(self):
        classify.wait_for_model_request(self.URL, self.MODEL, self.PROVIDER)
        self.now += 30
        classify.wait_for_model_request(self.URL, self.MODEL, self.PROVIDER)
        classify.wait_for_model_request(self.URL, self.MODEL, self.PROVIDER)
        self.assertEqual(self.sleeps, [3.0])
        classify.wait_for_model_request(self.URL, 'x-ai/grok-4.6', {})
        classify.wait_for_model_request('http://localhost/v1/chat/completions', self.MODEL, {})
        self.assertEqual(self.sleeps, [3.0])

    def test_initial_attempt_and_retry_are_both_paced(self):
        starts = []
        result_body = json.dumps({'choices': [{'message': {'content': '{"action":"trash"}'}}]}).encode()

        def response(request, **kwargs):
            starts.append(self.now)
            if len(starts) == 1:
                raise urllib.error.HTTPError(self.URL, 503, 'Unavailable', {}, io.BytesIO())
            return io.BytesIO(result_body)

        with patch('reply_rules.get_rules', return_value=[]), patch.object(
                classify.urllib.request, 'urlopen', side_effect=response):
            result = classify.classify_one(self.URL, {}, self.MODEL, 'Classify.', {'id': 'synthetic'}, retries=1)
        self.assertEqual(result['action'], 'trash')
        self.assertEqual(starts, [100.0, 103.0])

    def test_free_route_has_independent_pacing_and_invalid_intervals_fail(self):
        free_model = 'inclusionai/ling-3.0-flash-vl:free'
        with patch.dict(os.environ, {'TAHOR_FREE_REQUEST_INTERVAL_SECONDS': '5'}):
            classify.wait_for_model_request(self.URL, free_model, {'only': ['novita']})
            classify.wait_for_model_request(self.URL, self.MODEL, self.PROVIDER)
            self.assertEqual(self.sleeps, [])
            classify.wait_for_model_request(self.URL, free_model, {'only': ['novita']})
            self.assertEqual(self.sleeps, [5.0])
        for value in ('nan', 'inf', '0', '61'):
            with patch.dict(os.environ, {'TAHOR_FREE_REQUEST_INTERVAL_SECONDS': value}):
                with self.assertRaises(ValueError):
                    classify.wait_for_model_request(self.URL, free_model, {'only': ['novita']})

    def test_interval_bounds_and_explicit_configuration(self):
        for value in ('0', '-1', '61', 'nan', 'inf', 'invalid'):
            with self.subTest(value=value), patch.dict(os.environ, {'TAHOR_PAID_REQUEST_INTERVAL_SECONDS': value}):
                with self.assertRaises(ValueError):
                    classify.paid_request_interval()
        with patch.dict(os.environ, {'TAHOR_PAID_REQUEST_INTERVAL_SECONDS': '1.5'}):
            self.assertEqual(classify.paid_request_interval(), 1.5)
