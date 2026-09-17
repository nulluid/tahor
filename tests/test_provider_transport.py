from pathlib import Path
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

from provider_connector.auth import FastmailAuth, ProtocolError, RateLimited, ConnectorError, MAX_RESPONSE


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Mock()
        self.auth = FastmailAuth(Path(self.temp.name)/'missing', Path(self.temp.name)/'state', transport=self.transport)

    def response(self, status, content):
        response = Mock(status_code=status)
        response.raw = io.BytesIO(content)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        self.transport.request.return_value = context
        return response

    def test_redirect_is_never_followed_and_proxy_credentials_ignored(self):
        self.response(302, b'')
        with self.assertRaises(ProtocolError):
            self.auth.request('POST', 'https://api.fastmail.com/auth/login', {'type':'start'})
        kwargs = self.transport.request.call_args.kwargs
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (10,45))
        self.assertFalse(self.transport.trust_env)

    def test_oversized_and_non_json_provider_responses_stop(self):
        for content in (b'x'*(MAX_RESPONSE+1), b'not json'):
            self.response(200, content)
            with self.assertRaises(ProtocolError):
                self.auth.request('GET','https://api.fastmail.com/auth/sessions')

    def test_rate_limit_does_not_expose_response_body(self):
        response = self.response(429, b'secret')
        with self.assertRaises(RateLimited) as caught:
            self.auth.request('GET','https://api.fastmail.com/auth/sessions')
        self.assertEqual(str(caught.exception), '')
        self.assertEqual(response.raw.tell(), 0)

    def test_slow_trickle_has_elapsed_deadline_and_closes_response(self):
        response = self.response(200, b'')
        clock = [0]
        def read1(size):
            clock[0] += 31
            return b' '
        response.raw = Mock()
        response.raw.read1.side_effect = read1
        with patch('provider_connector.auth.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(ConnectorError) as caught:
                self.auth.request('GET', 'https://api.fastmail.com/auth/sessions')
        self.assertEqual(str(caught.exception), '')
        self.assertEqual(response.raw.read1.call_count, 3)
        self.transport.request.return_value.__exit__.assert_called_once()
        self.assertTrue(response.raw.decode_content)

    def test_older_transport_without_read1_uses_bounded_single_byte_streaming(self):
        response = self.response(200, b'')
        response.raw = type('LegacyReader', (), {})()
        response.iter_content.return_value = iter([b'{', b'}'])
        self.assertEqual(self.auth.request('GET', 'https://api.fastmail.com/auth/sessions'), (200, {}))
        response.iter_content.assert_called_once_with(chunk_size=1)
