import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import classify


class FreeGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'free_classifier_guidance.txt'
        env = patch.dict(os.environ, {'DATA_DIR': str(self.root), 'TAHOR_FREE_CLASSIFIER_GUIDANCE_PATH': '',
                                      'TAHOR_CLASSIFY_FREE_ENABLED': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.backend = classify.BACKENDS['openrouter-free']

    def call(self, backend=None):
        backend = backend or self.backend
        captured = []
        def response(request, **kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': '{"action":"trash"}'}}]}).encode())
        with patch('reply_rules.get_rules', return_value=[]), patch('reply_rules.classification_prompt',
                  side_effect=lambda prompt, rules: prompt + '\nRULES') as rules, patch.object(
                  classify.urllib.request, 'urlopen', side_effect=response) as http, patch.object(classify, 'wait_for_model_request'):
            result = classify.classify_one(backend['url'], {}, backend['default_model'], 'BASE', {'id': 'sample'}, retries=0)
        return result, captured, rules, http

    def test_free_guidance_precedes_rule_injection_and_preserves_exact_text(self):
        self.path.write_text('Preserve uncertainty.\n')
        result, payloads, rules, _ = self.call()
        self.assertEqual(result['action'], 'trash')
        rules.assert_called_once_with('BASE\n\nPreserve uncertainty.\n', [])
        self.assertEqual(payloads[0]['messages'][0]['content'], 'BASE\n\nPreserve uncertainty.\n\nRULES')

    def test_paid_prompt_unchanged_even_when_guidance_is_invalid(self):
        self.path.write_bytes(b'\xff')
        _, payloads, _, _ = self.call(classify.BACKENDS['openrouter-paid'])
        self.assertEqual(payloads[0]['messages'][0]['content'], 'BASE\nRULES')

    def test_missing_optional_file_preserves_default_prompt(self):
        _, payloads, _, _ = self.call()
        self.assertEqual(payloads[0]['messages'][0]['content'], 'BASE\nRULES')

    def test_explicit_override_and_exact_size_boundary(self):
        other = self.root / 'alternate.txt'
        other.write_bytes(b'x' * classify.FREE_GUIDANCE_MAX_BYTES)
        with patch.dict(os.environ, {'TAHOR_FREE_CLASSIFIER_GUIDANCE_PATH': str(other)}):
            result, payloads, _, _ = self.call()
        self.assertEqual(result['action'], 'trash')
        self.assertEqual(len(payloads[0]['messages'][0]['content']), len('BASE\n\n\nRULES') + classify.FREE_GUIDANCE_MAX_BYTES)

    def test_oversized_invalid_and_unreadable_fail_closed_without_private_error_details(self):
        for content in (b'x' * (classify.FREE_GUIDANCE_MAX_BYTES + 1), b'\xff'):
            self.path.write_bytes(content)
            result, _, rules, http = self.call()
            self.assertEqual(result['action'], 'error')
            rules.assert_not_called()
            http.assert_not_called()
        with patch.object(classify.os, 'open', side_effect=PermissionError('PRIVATE FILE CONTENT')):
            result, _, _, http = self.call()
        http.assert_not_called()
        self.assertNotIn('PRIVATE', result['reason'])

    def test_missing_explicit_override_fails_closed(self):
        with patch.dict(os.environ, {'TAHOR_FREE_CLASSIFIER_GUIDANCE_PATH': str(self.root / 'absent')}):
            result, _, _, http = self.call()
        self.assertEqual(result['action'], 'error')
        http.assert_not_called()

    def test_nonregular_file_and_symlink_fail_closed(self):
        self.path.mkdir()
        result, _, _, http = self.call()
        self.assertEqual(result['action'], 'error')
        http.assert_not_called()
        self.path.rmdir()
        target = self.root / 'target'
        target.write_text('Private guidance')
        self.path.symlink_to(target)
        if hasattr(os, 'O_NOFOLLOW'):
            result, _, _, http = self.call()
            self.assertEqual(result['action'], 'error')
            http.assert_not_called()
