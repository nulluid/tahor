from pathlib import Path
import socket
import io
import os
import smtplib
import urllib.error
import ssl
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unsubscribe


class UnsubscribeTests(unittest.TestCase):
    def test_dns_rebinding_is_rejected_before_connect(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
        with patch.object(socket, 'getaddrinfo', side_effect=[public, private]), patch.object(socket, 'socket') as create:
            unsubscribe.validate_url('http://example.com/unsubscribe')
            with self.assertRaises(ValueError):
                unsubscribe.PublicHTTPConnection('example.com', timeout=15).connect()
            create.assert_not_called()

    def test_connection_uses_validated_address_and_original_tls_hostname(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        context = Mock()
        with patch.object(socket, 'getaddrinfo', return_value=addresses) as resolve, patch.object(socket, 'socket') as create:
            conn = unsubscribe.PublicHTTPSConnection('example.com', timeout=15, context=context)
            conn.connect()
            create.return_value.connect.assert_called_once_with(('93.184.216.34', 443))
            context.wrap_socket.assert_called_once_with(create.return_value, server_hostname='example.com')
            resolve.assert_called_once()
        secure = unsubscribe.PublicHTTPSConnection('example.com')
        self.assertTrue(secure._context.check_hostname)
        self.assertEqual(secure._context.verify_mode, ssl.CERT_REQUIRED)

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
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'leave@example.com')
        self.assertEqual(message['Subject'], 'Remove me')
        self.assertEqual(message.get_content().strip(), 'please')

    def test_no_mechanism_is_not_reported_as_success(self):
        with self.assertRaises(unsubscribe.UnsubscribeError):
            unsubscribe.execute(dict(unsubscribe_url=None, unsubscribe_mailto=None, one_click=False), '', '', '', 465)

    def test_smtp_requires_verified_tls_and_supports_separate_sending_credential(self):
        candidate = dict(unsubscribe_url=None, unsubscribe_mailto='leave@example.com', one_click=False)
        with patch.dict(os.environ, {'FASTMAIL_SMTP_USERNAME': 'login@example.com', 'FASTMAIL_SMTP_APP_PASSWORD': 'smtp-only-secret'}), patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            session = smtp.return_value.__enter__.return_value
            session.send_message.return_value = {}
            unsubscribe.execute(candidate, 'owner@example.com', 'imap-only-secret', 'smtp.example.com', 465)
            session.login.assert_called_once_with('login@example.com', 'smtp-only-secret')
            context = smtp.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(session.send_message.call_args.args[0]['From'], 'owner@example.com')

    def test_smtp_authentication_failure_is_actionable_without_provider_secrets(self):
        candidate = dict(unsubscribe_url=None, unsubscribe_mailto='tokenized-address@example.com', one_click=False)
        with patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'PRIVATE PROVIDER RESPONSE token=secret')
            with self.assertRaises(unsubscribe.UnsubscribeError) as error:
                unsubscribe.execute(candidate, 'owner@example.com', 'private-password', 'smtp.example.com', 465)
            smtp.return_value.__enter__.return_value.send_message.assert_not_called()
        text = str(error.exception)
        self.assertIn('Mail (IMAP/POP/SMTP)', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('private-password', text)
        self.assertNotIn('tokenized-address', text)

    def test_explicit_http_403_uses_only_advertised_mailto_alternative(self):
        candidate = dict(unsubscribe_url='https://example.com/private-token', unsubscribe_mailto='leave@example.com?subject=unsubscribe', one_click=True)
        rejected = urllib.error.HTTPError(candidate['unsubscribe_url'], 403, 'PRIVATE BODY', {}, None)
        with patch.object(unsubscribe, 'open_public', side_effect=rejected), patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            result = unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
            self.assertIn('advertised email method', result)
            smtp.return_value.__enter__.return_value.send_message.assert_called_once()

    def test_timeout_or_non_403_error_never_sends_second_transport_request(self):
        candidate = dict(unsubscribe_url='https://example.com/private-token', unsubscribe_mailto='leave@example.com', one_click=True)
        for error in (TimeoutError('PRIVATE'), urllib.error.HTTPError(candidate['unsubscribe_url'], 500, 'PRIVATE', {}, None)):
            with self.subTest(error=type(error).__name__), patch.object(unsubscribe, 'open_public', side_effect=error), patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
                with self.assertRaises(unsubscribe.UnsubscribeError) as failure:
                    unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
                smtp.assert_not_called()
                self.assertNotIn('PRIVATE', str(failure.exception))
                self.assertNotIn('private-token', str(failure.exception))

    def test_403_without_mailto_requires_manual_action_and_never_claims_success(self):
        candidate = dict(unsubscribe_url='https://example.com/private-token', unsubscribe_mailto=None, one_click=True)
        error = urllib.error.HTTPError(candidate['unsubscribe_url'], 403, 'PRIVATE', {}, None)
        with patch.object(unsubscribe, 'open_public', side_effect=error), patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            with self.assertRaisesRegex(unsubscribe.UnsubscribeError, 'Open its unsubscribe page'):
                unsubscribe.execute(candidate, '', '', '', 465)
            smtp.assert_not_called()

    def test_generic_link_requires_confirmation_without_issuing_get(self):
        candidate = dict(unsubscribe_url='https://example.com/private-token', unsubscribe_mailto=None, one_click=False)
        with patch.object(unsubscribe, 'validate_url'), patch.object(unsubscribe, 'open_public') as opener:
            with self.assertRaises(unsubscribe.UnsubscribeError) as error:
                unsubscribe.execute(candidate, '', '', '', 465)
            self.assertEqual(error.exception.code, 'manual_confirmation')
            opener.assert_not_called()

    def test_mailto_fallback_rejects_header_injection_before_connect(self):
        candidate = dict(unsubscribe_url='https://example.com/u', unsubscribe_mailto='leave@example.com?subject=unsubscribe%0d%0aBcc%3asecret', one_click=True)
        rejected = urllib.error.HTTPError(candidate['unsubscribe_url'], 403, 'Forbidden', {}, None)
        with patch.object(unsubscribe, 'open_public', side_effect=rejected), patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            with self.assertRaises(unsubscribe.UnsubscribeError):
                unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
            smtp.assert_not_called()

    def test_smtp_recipient_rejection_is_not_success(self):
        candidate = dict(unsubscribe_url=None, unsubscribe_mailto='leave@example.com', one_click=False)
        with patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {'leave@example.com': (550, b'PRIVATE')}
            with self.assertRaisesRegex(unsubscribe.UnsubscribeError, 'address was rejected'):
                unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)

    def test_database_wrapper_uses_configured_smtp_host(self):
        import tahor_db
        import config
        with patch.object(config, 'SMTP_HOST', 'smtp.other.example'), patch.object(unsubscribe, 'execute', return_value='Submitted') as execute:
            self.assertEqual(tahor_db.execute_unsubscribe({}, 'owner@example.com', 'secret'), 'Submitted')
        self.assertEqual(execute.call_args.args[-2:], ('smtp.other.example', 465))

    def test_http_one_click_tries_same_endpoint_over_https_before_mailto(self):
        candidate = dict(unsubscribe_url='http://example.com:80/u?opaque=secret', unsubscribe_mailto='leave@example.com', one_click=True)
        response = Mock(status=200)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(unsubscribe, 'open_public', return_value=context) as opener, patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            self.assertIn('HTTP 200', unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465))
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, 'https://example.com/u?opaque=secret')
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.data, b'List-Unsubscribe=One-Click')
            smtp.assert_not_called()

    def test_custom_http_port_without_mailto_requires_manual_confirmation(self):
        candidate = dict(unsubscribe_url='http://example.com:8080/u', unsubscribe_mailto=None, one_click=True)
        with patch.object(unsubscribe, 'validate_url'), patch.object(unsubscribe, 'open_public') as opener:
            with self.assertRaises(unsubscribe.UnsubscribeError) as error:
                unsubscribe.execute(candidate, '', '', '', 465)
            self.assertEqual(error.exception.code, 'manual_confirmation')
            self.assertIn('custom port', str(error.exception))
            opener.assert_not_called()

    def test_https_upgrade_never_downgrades_via_redirect(self):
        request = unsubscribe.urllib.request.Request('https://example.com/u', data=b'List-Unsubscribe=One-Click', method='POST')
        with patch.object(unsubscribe, 'validate_url') as validate:
            with self.assertRaises(ValueError):
                unsubscribe.PublicRedirect().redirect_request(request, None, 302, 'Found', {}, 'http://example.com/u')
            validate.assert_not_called()

    def test_https_upgrade_rejection_can_use_advertised_mailto(self):
        candidate = dict(unsubscribe_url='http://example.com/u', unsubscribe_mailto='leave@example.com', one_click=True)
        error = urllib.error.HTTPError('https://example.com/u', 405, 'PRIVATE', {}, None)
        with patch.object(unsubscribe, 'open_public', side_effect=error) as opener, patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertIn('HTTP 405', unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465))
            self.assertEqual(opener.call_args.args[0].full_url, 'https://example.com/u')
            smtp.return_value.__enter__.return_value.send_message.assert_called_once()

    def test_https_upgrade_timeout_never_retries_insecurely_or_sends_email(self):
        candidate = dict(unsubscribe_url='http://example.com/u', unsubscribe_mailto='leave@example.com', one_click=True)
        with patch.object(unsubscribe, 'open_public', side_effect=TimeoutError('PRIVATE')) as opener, patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            with self.assertRaises(unsubscribe.UnsubscribeError):
                unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(opener.call_args.args[0].full_url, 'https://example.com/u')
            smtp.assert_not_called()

    def test_unsafe_one_click_scheme_never_uses_network(self):
        candidate = dict(unsubscribe_url='file:///etc/passwd', unsubscribe_mailto='leave@example.com', one_click=True)
        with patch.object(unsubscribe, 'open_public') as opener, patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            with self.assertRaises(unsubscribe.UnsubscribeError):
                unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
            opener.assert_not_called()
            smtp.assert_not_called()

    def test_quit_failure_after_smtp_acceptance_does_not_invite_duplicate_send(self):
        candidate = dict(unsubscribe_url=None, unsubscribe_mailto='leave@example.com', one_click=False)
        with patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            smtp.return_value.__exit__.side_effect = smtplib.SMTPServerDisconnected('PRIVATE')
            self.assertIn('email submitted', unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465))

    def test_https_upgrade_requires_valid_certificate_and_never_falls_back_to_http(self):
        candidate = dict(unsubscribe_url='http://example.com/u', unsubscribe_mailto='leave@example.com', one_click=True)
        with patch.object(unsubscribe, 'open_public', side_effect=ssl.SSLCertVerificationError('PRIVATE')) as opener, patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            with self.assertRaisesRegex(unsubscribe.UnsubscribeError, 'TLS verification'):
                unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465)
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(opener.call_args.args[0].full_url, 'https://example.com/u')
            smtp.assert_not_called()

    def test_custom_http_port_uses_advertised_mailto_without_changing_service(self):
        candidate = dict(unsubscribe_url='http://example.com:8080/u', unsubscribe_mailto='leave@example.com', one_click=True)
        with patch.object(unsubscribe, 'open_public') as opener, patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertIn('email submitted', unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465))
            opener.assert_not_called()

    def test_http_rejection_closes_real_body_before_permitted_fallback(self):
        candidate = dict(unsubscribe_url='https://example.com/u', unsubscribe_mailto='leave@example.com', one_click=True)
        body = io.BytesIO(b'PRIVATE RESPONSE')
        rejected = urllib.error.HTTPError(candidate['unsubscribe_url'], 403, 'Forbidden', {}, body)
        with patch.object(unsubscribe, 'open_public', side_effect=rejected), patch.object(unsubscribe.smtplib, 'SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertIn('email submitted', unsubscribe.execute(candidate, 'owner@example.com', 'secret', 'smtp.example.com', 465))
        self.assertTrue(body.closed)

    def test_http_error_cleanup_failure_does_not_mask_definite_rejection(self):
        candidate = dict(unsubscribe_url='https://example.com/u', unsubscribe_mailto=None, one_click=True)
        rejected = urllib.error.HTTPError(candidate['unsubscribe_url'], 403, 'Forbidden', {}, io.BytesIO())
        with patch.object(rejected, 'close', side_effect=AttributeError('body absent')) as close, patch.object(unsubscribe, 'open_public', side_effect=rejected):
            with self.assertRaisesRegex(unsubscribe.UnsubscribeError, 'HTTP 403'):
                unsubscribe.execute(candidate, '', '', '', 465)
        close.assert_called_once()
        rejected.close()
