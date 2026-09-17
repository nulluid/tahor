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
                raise urllib.error.HTTPError(request.full_url, 503, 'Unavailable', {}, io.BytesIO(b''))
            return io.BytesIO(result_body)

        record = {'id': 'sample', 'subject': 'Question', 'from': 'person@example.com',
                  'date': '2026-01-01', 'snippet': 'Can you attend?'}
        with patch.object(classify, 'wait_for_model_request'), patch.dict(classify.os.environ, {'TAHOR_CLASSIFY_FREE_ENABLED': '1'}), patch('reply_rules.get_rules', return_value=[]), patch.object(
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
        self.assertEqual(payload['provider'], {'only': ['google-vertex/global'],
                         'allow_fallbacks': False, 'zdr': True, 'data_collection': 'deny'})
        self.assertEqual(payload['model'], 'google/gemini-3.8-flash')
        self.assertEqual(payload['messages'][0],
                         {'role': 'system', 'content': 'Classify the message.'})
        self.assertEqual(payload['messages'][1]['content'],
                         'Subject: Question\nFrom: person@example.com\nDate: 2026-01-01\n'
                         'Body/snippet: Can you attend?')
        self.assertEqual(payload['temperature'], 0.1)
        self.assertEqual(payload['max_tokens'], 2048)
        self.assertEqual(payload['reasoning'], {'effort': 'low'})

    def test_free_requires_privacy_and_local_stays_local(self):
        for backend_name in ('openrouter-free', 'local'):
            with self.subTest(backend=backend_name):
                payload = self.classify_with_capture(backend_name)[0]
                if backend_name == 'openrouter-free':
                    self.assertEqual(payload['provider'], {'only': ['novita'], 'allow_fallbacks': False,
                                     'max_price': {'prompt': 0, 'completion': 0},
                                     'zdr': True, 'data_collection': 'deny'})
                else:
                    self.assertNotIn('provider', payload)
                self.assertEqual(payload['model'], classify.BACKENDS[backend_name]['default_model'])

    def test_unverified_direct_google_account_sends_no_mail_content(self):
        backend = classify.BACKENDS['gemini']
        with patch.object(classify.urllib.request, 'urlopen') as request:
            result = classify.classify_one(backend['url'], {}, backend['default_model'],
                                          'Classify.', {'id': 'private-source', 'snippet': 'Private text'})
        request.assert_not_called()
        self.assertEqual(result['action'], 'error')
        self.assertNotIn('Private text', result['reason'])

    def test_backend_options_cannot_relax_privacy_on_initial_request_or_retry(self):
        options = {'provider': {'only': ['synthetic-provider'], 'allow_fallbacks': False,
                                'zdr': False, 'data_collection': 'allow'},
                   'reasoning': {'enabled': False}, 'max_tokens': 2048}
        with patch.dict(classify.BACKENDS['openrouter-paid'], {'request_options': options}):
            requests = self.classify_with_capture('openrouter-paid', fail_first=True)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0]['provider'], {'only': ['synthetic-provider'],
                         'allow_fallbacks': False, 'zdr': True, 'data_collection': 'deny'})
        self.assertEqual(requests[0]['max_tokens'], 2048)
        self.assertEqual(requests[0]['reasoning'], {'enabled': False})
