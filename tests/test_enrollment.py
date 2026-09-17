import contextlib
import io
import unittest
import warnings
from unittest.mock import patch
from scripts import enroll_fastmail as enrollment

SEED='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'  # Published RFC test vector, not a credential.

class EnrollmentTests(unittest.TestCase):
    def test_seed_checked_before_password_and_only_invalid_field_retried(self):
        output=io.StringIO()
        with patch('builtins.input',side_effect=['bad','owner@example.com']) as username, patch.object(enrollment.getpass,'getpass',side_effect=['123456',SEED,'private synthetic password']) as secret, contextlib.redirect_stdout(output):
            value=enrollment.collect_credentials()
        self.assertEqual(username.call_count,2)
        self.assertIn('setup key',secret.call_args_list[0].args[0])
        self.assertIn('setup key',secret.call_args_list[1].args[0])
        self.assertIn('account password',secret.call_args_list[2].args[0])
        self.assertEqual(value['totp_seed'],SEED)
        self.assertEqual(value['password'],'private synthetic password')
        self.assertIn('Username is invalid',output.getvalue())
        self.assertIn('Authenticator setup key is invalid',output.getvalue())
        for secret_value in (SEED,'123456','private synthetic password'):
            self.assertNotIn(secret_value,output.getvalue())

    def test_invalid_seed_exhaustion_never_requests_password_or_writes(self):
        with patch('builtins.input',return_value='owner@example.com'), patch.object(enrollment.getpass,'getpass',return_value='invalid secret') as secret, patch.object(enrollment.subprocess,'run') as command, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(SystemExit,'nothing saved'):
                enrollment.collect_credentials()
        self.assertEqual(secret.call_count,3)
        self.assertTrue(all('setup key' in call.args[0] for call in secret.call_args_list))
        command.assert_not_called()
        self.assertNotIn('invalid secret',output.getvalue())

    def test_password_reprompt_preserves_seed_and_does_not_strip_password(self):
        with patch('builtins.input',return_value='owner@example.com'), patch.object(enrollment.getpass,'getpass',side_effect=[SEED,'',' padded synthetic password ']), contextlib.redirect_stdout(io.StringIO()):
            value=enrollment.collect_credentials()
        self.assertEqual(value['password'],' padded synthetic password ')

    def test_echo_fallback_is_rejected_before_reading_secret(self):
        def warning(prompt):
            warnings.warn('Cannot control echo',enrollment.getpass.GetPassWarning)
            self.fail('Should never fall back to echoed input')
        with patch.object(enrollment.getpass,'getpass',side_effect=warning):
            with self.assertRaisesRegex(SystemExit,'Hidden terminal input is unavailable'):
                enrollment.hidden_input('Secret: ')

    def test_cancel_during_hidden_input_has_no_secret_traceback(self):
        with patch('builtins.input',return_value='owner@example.com'), patch.object(enrollment.getpass,'getpass',side_effect=KeyboardInterrupt), self.assertRaisesRegex(SystemExit,'cancelled; nothing saved'):
            enrollment.collect_credentials()
