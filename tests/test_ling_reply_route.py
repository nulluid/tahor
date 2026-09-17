import io
import json
import os
import unittest
from unittest.mock import patch

import draft_replies
import mailbox_settings


class LingReplyRouteTests(unittest.TestCase):
    def test_generation_and_verifier_require_free_novita_privacy_route(self):
        contents = [
            {'sentences': ['Thank you for the update.', 'I hope the event goes well.']},
            {'approved': True, 'issues': [], 'needs_attention': False},
        ]
        requests = []

        def respond(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices': [{'message': {
                'content': json.dumps(contents.pop(0)),
            }}]}).encode())

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic'}), patch.object(
                draft_replies.config, 'email_address', return_value='owner@example.com'), patch.object(
                draft_replies.urllib.request, 'urlopen', side_effect=respond):
            body = draft_replies._draft_reply_body(
                'Update', 'person@example.com', 'Our volunteer event is tomorrow.',
                {'instructions': 'Thank the writer and wish them well.', 'max_sentences': 3},
                key='ling-free')
        self.assertEqual(body, 'Thank you for the update. I hope the event goes well.')
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request['model'], 'inclusionai/ling-3.0-flash-vl:free')
            self.assertEqual(request['reasoning'], {'enabled': False})
            self.assertEqual(request['provider'], {
                'only': ['novita'], 'allow_fallbacks': False, 'zdr': True,
                'data_collection': 'deny', 'max_price': {'prompt': 0, 'completion': 0},
            })
        self.assertIn('candidate_reply', json.loads(requests[1]['messages'][1]['content']))

    def test_route_is_eligible_backup_without_changing_fresh_defaults(self):
        self.assertIn('ling-free', mailbox_settings.free_reply_models())
        self.assertEqual(mailbox_settings.DEFAULT_REPLY_MODEL, 'nemotron-free')
        self.assertEqual(mailbox_settings.DEFAULT_SETTINGS['reply_backup_model'], 'nemotron-free')
