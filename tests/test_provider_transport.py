from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from provider_connector.auth import FastmailAuth, ProtocolError, RateLimited, MAX_RESPONSE


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Mock()
        self.auth = FastmailAuth(Path(self.temp.name)/'missing', Path(self.temp.name)/'state', transport=self.transport)

    def response(self, status, content):
        response = Mock(status_code=status)
        response.iter_content.return_value = [content]
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
        response.iter_content.assert_not_called()
