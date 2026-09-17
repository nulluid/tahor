"""Private terminal SMTP enrollment never sends mail or saves rejected credentials."""
import contextlib
import getpass
import importlib.util
import io
import os
from pathlib import Path
import smtplib
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
import warnings

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'enroll_smtp.py'
spec = importlib.util.spec_from_file_location('smtp_enrollment', MODULE)
enrollment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enrollment)


class SMTPEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.path = self.root / 'config.env'
        self.original = ('# Existing private configuration\nFASTMAIL_EMAIL="owner@example.com"\n'
                         'FASTMAIL_APP_PASSWORD="existing-imap-secret"\nFASTMAIL_SMTP_HOST="smtp.example.com"\nOTHER_SETTING="unchanged"\n')
        self.path.write_text(self.original)
        self.path.chmod(0o640)
        self.smtp = self.start(patch.object(enrollment.smtplib, 'SMTP_SSL'))
        self.session = self.smtp.return_value.__enter__.return_value
        self.session.esmtp_features = {'auth': 'PLAIN LOGIN'}
        self.session.auth.return_value = (235, b'Accepted')
        self.start(patch('builtins.input', return_value=''))
        self.start(patch.object(enrollment, 'hidden_input', return_value='new-smtp-secret'))

    def start(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def enroll(self):
        enrollment.enroll(self.path, owner_uid=os.getuid())

    def test_verified_auth_only_atomically_preserves_imap_settings_owner_group_and_mode(self):
        before = self.path.stat()
        self.enroll()
        values = enrollment.parse_environment(self.path.read_text())
        self.assertEqual(values['FASTMAIL_SMTP_USERNAME'], 'owner@example.com')
        self.assertEqual(values['FASTMAIL_SMTP_APP_PASSWORD'], 'new-smtp-secret')
        self.assertEqual(values['FASTMAIL_APP_PASSWORD'], 'existing-imap-secret')
        self.assertEqual(values['OTHER_SETTING'], 'unchanged')
        after = self.path.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode & 0o777), (before.st_uid, before.st_gid, 0o640))
        self.assertNotEqual(after.st_ino, before.st_ino)
        context = self.smtp.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.smtp.assert_called_once()
        self.session.auth.assert_called_once()
        self.assertEqual(self.session.auth.call_args.args[0], 'PLAIN')
        self.session.login.assert_not_called()
        self.session.send_message.assert_not_called()
        self.session.sendmail.assert_not_called()

    def test_rejected_credentials_leave_config_unchanged_and_are_never_printed(self):
        self.session.auth.side_effect = smtplib.SMTPAuthenticationError(535, b'PRIVATE PROVIDER RESPONSE')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(enrollment.EnrollmentError) as failure:
            self.enroll()
        self.assertEqual(self.path.read_text(), self.original)
        self.assertIn('nothing saved', str(failure.exception))
        for secret in ('new-smtp-secret', 'existing-imap-secret', 'PRIVATE PROVIDER RESPONSE'):
            self.assertNotIn(secret, output.getvalue() + str(failure.exception))
        self.session.auth.assert_called_once()

    def test_only_login_mechanism_is_one_exchange_without_fallback(self):
        self.session.esmtp_features = {'auth': 'LOGIN'}
        self.enroll()
        self.session.auth.assert_called_once_with('LOGIN', self.session.auth_login, initial_response_ok=False)
        self.assertEqual(self.session.user, 'owner@example.com')

    def test_certificate_failure_does_not_save_or_try_another_connection(self):
        self.smtp.side_effect = ssl.SSLCertVerificationError('PRIVATE')
        with self.assertRaises(enrollment.EnrollmentError):
            self.enroll()
        self.smtp.assert_called_once()
        self.assertEqual(self.path.read_text(), self.original)

    def test_already_authenticated_is_not_verification_of_supplied_password(self):
        self.session.auth.return_value = (503, b'Already authenticated')
        with self.assertRaises(enrollment.EnrollmentError):
            self.enroll()
        self.assertEqual(self.path.read_text(), self.original)

    def test_symlink_file_or_parent_is_rejected_before_authentication(self):
        actual = self.root / 'actual.env'
        self.path.rename(actual)
        self.path.symlink_to(actual)
        with self.assertRaises((OSError, enrollment.EnrollmentError)):
            self.enroll()
        self.path.unlink()
        self.path = self.root / 'linked' / 'config.env'
        target = self.root / 'directory'
        target.mkdir()
        (self.root / 'linked').symlink_to(target, target_is_directory=True)
        with self.assertRaises((OSError, enrollment.EnrollmentError)):
            self.enroll()
        self.smtp.assert_not_called()
        self.assertEqual(actual.read_text(), self.original)

    def test_public_permissions_are_rejected_before_authentication(self):
        self.path.chmod(0o644)
        with self.assertRaises(enrollment.EnrollmentError):
            self.enroll()
        self.smtp.assert_not_called()

    def test_configuration_change_during_auth_is_not_overwritten(self):
        def change(*args, **kwargs):
            self.path.write_text(self.original + 'NEW_SETTING="keep this"\n')
            return (235, b'Accepted')
        self.session.auth.side_effect = change
        with self.assertRaisesRegex(enrollment.EnrollmentError, 'configuration changed'):
            self.enroll()
        self.assertIn('NEW_SETTING', self.path.read_text())
        self.assertNotIn('new-smtp-secret', self.path.read_text())

    def test_atomic_replace_failure_leaves_no_temporary_secret_file(self):
        with patch.object(enrollment.os, 'replace', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            self.enroll()
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(list(self.root.glob('.smtp-config-*')), [])

    def test_only_smtp_keys_are_replaced_and_quoted_values_round_trip(self):
        original = self.original + 'export FASTMAIL_SMTP_APP_PASSWORD="old"\nFASTMAIL_SMTP_APP_PASSWORD="duplicate"\n'
        password = 'spaces " quotes \\ slash $ dollar ` backtick'
        result = enrollment.update_environment(original, 'login@example.com', password)
        values = enrollment.parse_environment(result)
        self.assertEqual(values['FASTMAIL_SMTP_APP_PASSWORD'], password)
        self.assertEqual(result.count('FASTMAIL_SMTP_APP_PASSWORD='), 1)
        self.assertEqual(values['FASTMAIL_APP_PASSWORD'], 'existing-imap-secret')

    def test_hidden_input_refuses_echo_fallback(self):
        # Bypass the enrollment fixture's hidden-input stub for this helper test.
        actual_spec = importlib.util.spec_from_file_location('smtp_enrollment_hidden', MODULE)
        actual = importlib.util.module_from_spec(actual_spec)
        actual_spec.loader.exec_module(actual)
        def insecure(prompt):
            warnings.warn('No echo suppression', getpass.GetPassWarning)
            return 'should-never-be-accepted'
        with patch.object(actual.getpass, 'getpass', side_effect=insecure), self.assertRaises(actual.EnrollmentError):
            actual.hidden_input('Private: ')

    def test_main_requires_root_terminal_and_never_accepts_secret_arguments(self):
        for arguments, uid, terminal in [(['enroll_smtp.py', 'private-secret'], 0, True),
                                         (['enroll_smtp.py'], 1000, True),
                                         (['enroll_smtp.py'], 0, False)]:
            with self.subTest(uid=uid, terminal=terminal), patch.object(enrollment.sys, 'argv', arguments), patch.object(enrollment.os, 'geteuid', return_value=uid), patch.object(enrollment.sys.stdin, 'isatty', return_value=terminal):
                with self.assertRaises(SystemExit) as failure:
                    enrollment.main()
            self.assertNotIn('private-secret', str(failure.exception))
        self.smtp.assert_not_called()

    def test_hardlinked_configuration_is_rejected_before_authentication(self):
        os.link(self.path, self.root / 'another-name.env')
        with self.assertRaises(enrollment.EnrollmentError):
            self.enroll()
        self.smtp.assert_not_called()

    def test_symlink_enrollment_lock_is_rejected_before_authentication(self):
        target = self.root / 'untouched'
        target.write_text('Keep this content')
        (self.root / '.smtp-enrollment.lock').symlink_to(target)
        with self.assertRaises((OSError, enrollment.EnrollmentError)):
            self.enroll()
        self.smtp.assert_not_called()
        self.assertEqual(target.read_text(), 'Keep this content')

    def test_owner_only_configuration_mode_is_preserved(self):
        self.path.chmod(0o600)
        self.enroll()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_main_restarts_only_web_after_verified_enrollment(self):
        output = io.StringIO()
        with patch.object(enrollment.sys, 'argv', ['enroll_smtp.py']), patch.object(enrollment.os, 'geteuid', return_value=0), patch.object(enrollment.sys.stdin, 'isatty', return_value=True), patch.object(enrollment, 'enroll') as enroll, patch.object(enrollment.os, 'umask'), patch.object(enrollment.subprocess, 'run') as restart, contextlib.redirect_stdout(output):
            enrollment.main()
        enroll.assert_called_once_with()
        restart.assert_called_once_with(['systemctl', 'restart', 'tahor-decision-app.service'], check=True, timeout=60, capture_output=True)
        self.assertIn('No email was sent', output.getvalue())
        self.assertNotIn('new-smtp-secret', output.getvalue())
