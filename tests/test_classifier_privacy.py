import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import classify


class ClassifierPrivacyTests(unittest.TestCase):
    def classify_with_capture(self, backend_name, fail_first=False):
        backend = classify.BACKENDS[backend_name]
        requests = []
        result_body = json.dumps({'choices': [{'message': {'content': json.dumps({
            'action': 'keep', 'category': 'personal', 'retention': 'standard',
            'expense_type': 'n/a', 'needs_attention': True,
        })}}]}).encode()

        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            if fail_first and len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 503, 'Unavailable', {}, None)
            return io.BytesIO(result_body)

        record = {'id': 'sample', 'subject': 'Question', 'from': 'person@example.com',
                  'date': '2026-01-01', 'snippet': 'Can you attend?'}
        with patch('reply_rules.get_rules', return_value=[]), patch.object(
                classify.urllib.request, 'urlopen', side_effect=response):
            result = classify.classify_one(backend['url'], {}, backend['default_model'],
                                           'Classify the message.', record, retries=1)
        self.assertEqual(result['action'], 'keep')
        return requests

    def test_paid_route_keeps_privacy_filters_and_classification_input_on_retry(self):
        requests = self.classify_with_capture('openrouter-paid', fail_first=True)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        payload = requests[0]
        self.assertEqual(payload['provider'], {'zdr': True, 'data_collection': 'deny'})
        self.assertEqual(payload['model'], 'nvidia/nemotron-3-super-120b-a12b')
        self.assertEqual(payload['messages'][0],
                         {'role': 'system', 'content': 'Classify the message.'})
        self.assertEqual(payload['messages'][1]['content'],
                         'Subject: Question\nFrom: person@example.com\nDate: 2026-01-01\n'
                         'Body/snippet: Can you attend?')
        self.assertEqual(payload['temperature'], 0.1)
        self.assertEqual(payload['max_tokens'], 1024)
        self.assertNotIn('reasoning', payload)

    def test_existing_free_and_non_openrouter_routes_are_unchanged(self):
        for backend_name in ('openrouter-free', 'local'):
            with self.subTest(backend=backend_name):
                payload = self.classify_with_capture(backend_name)[0]
                self.assertNotIn('provider', payload)
                self.assertEqual(payload['model'], classify.BACKENDS[backend_name]['default_model'])
