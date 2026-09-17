"""Connection setup does not open sockets without credentials or leak rejected logins."""
import imaplib
import unittest
from unittest.mock import Mock, patch
import fetch_batch

class ConnectionTests(unittest.TestCase):
    def test_missing_credentials_do_not_open_a_socket(self):
        with patch.object(fetch_batch.config, 'email_address', side_effect=SystemExit('Missing setting')), patch.object(fetch_batch.imaplib, 'IMAP4_SSL') as connect:
            with self.assertRaises(SystemExit):
                fetch_batch.connect()
        connect.assert_not_called()

    def test_rejected_login_closes_connection_before_retry(self):
        client = Mock()
        client.login.side_effect = imaplib.IMAP4.error('Rejected')
        with patch.object(fetch_batch.config, 'email_address', return_value='owner@example.com'), patch.object(fetch_batch.config, 'app_password', return_value='synthetic'), patch.object(fetch_batch.imaplib, 'IMAP4_SSL', return_value=client):
            with self.assertRaises(imaplib.IMAP4.error):
                fetch_batch.connect(timeout=5)
        client.shutdown.assert_called_once_with()
