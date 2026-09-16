from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unsubscribe


class UnsubscribeTests(unittest.TestCase):
    def test_private_and_link_local_urls_rejected(self):
        for address in ('127.0.0.1', '169.254.169.254', '10.0.0.1', '::1'):
            with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80))]):
                with self.assertRaises(ValueError):
                    unsubscribe.validate_url('http://example.com/unsubscribe')
        with self.assertRaises(ValueError):
            unsubscribe.validate_url('file:///etc/passwd')

    def test_one_click_uses_required_post_body(self):
        candidate = dict(unsubscribe_url='https://example.com/unsubscribe', unsubscribe_mailto=None, one_click=True)
        response = Mock(status=200)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(unsubscribe, 'open_public', return_value=context) as opener:
            unsubscribe.execute(candidate, '', '', '', 465)
        request = opener.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.data, b'List-Unsubscribe=One-Click')
        self.assertEqual(request.get_header('Content-type'), 'application/x-www-form-urlencoded')

    def test_mailto_subject_and_body_are_parsed(self):
        candidate = dict(unsubscribe_url=None, unsubscribe_mailto='leave@example.com?subject=Remove%20me&body=please', one_click=False)
        with patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'leave@example.com')
        self.assertEqual(message['Subject'], 'Remove me')
        self.assertEqual(message.get_content().strip(), 'please')

    def test_no_mechanism_is_not_reported_as_success(self):
        with self.assertRaises(ValueError):
            unsubscribe.execute(dict(unsubscribe_url=None, unsubscribe_mailto=None, one_click=False), '', '', '', 465)
