#!/usr/bin/env python3
"""Enroll a separate SMTP app password through an administrator's private terminal."""
from contextlib import contextmanager
import fcntl
import getpass
import os
from pathlib import Path
import re
import shlex
import smtplib
import ssl
import stat
import subprocess
import sys
import uuid
import warnings

CONFIG_PATH = Path('/etc/tahor/config.env')
MAX_CONFIG_BYTES = 1024 * 1024
SMTP_KEYS = {'FASTMAIL_SMTP_USERNAME', 'FASTMAIL_SMTP_APP_PASSWORD'}


class EnrollmentError(RuntimeError):
    pass


def hidden_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise EnrollmentError('Hidden terminal input is unavailable; nothing saved.') from None


def parse_environment(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise EnrollmentError('Private configuration needs repair; nothing saved.')
        try:
            parsed = shlex.split(value)
        except ValueError:
            raise EnrollmentError('Private configuration needs repair; nothing saved.') from None
        if len(parsed) > 1:
            raise EnrollmentError('Private configuration needs repair; nothing saved.')
        values[key] = parsed[0] if parsed else ''
    return values


def _quote(value):
    # Both run.load_environment and systemd EnvironmentFile accept these quoted
    # values without shell execution or variable expansion.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def update_environment(text, username, password):
    kept = []
    for line in text.splitlines(keepends=True):
        candidate = line.strip()
        if candidate.startswith('export '):
            candidate = candidate[7:]
        if candidate.partition('=')[0] not in SMTP_KEYS:
            kept.append(line)
    result = ''.join(kept)
    if result and not result.endswith('\n'):
        result += '\n'
    result += 'FASTMAIL_SMTP_USERNAME=' + _quote(username) + '\n'
    result += 'FASTMAIL_SMTP_APP_PASSWORD=' + _quote(password) + '\n'
    return result


def validate_credentials(username, password):
    if (not isinstance(username, str) or not 1 <= len(username) <= 320
            or username.count('@') != 1 or not username.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in username)):
        raise EnrollmentError('Enter the full SMTP sign-in email address; nothing saved.')
    if (not isinstance(password, str) or not 1 <= len(password) <= 4096
            or any(char in password for char in '\r\n\0')):
        raise EnrollmentError('The app password is empty or invalid; nothing saved.')


def verify_smtp(host, username, password):
    """One password-authentication attempt over verified TLS; never send mail."""
    authenticated = False
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=15, context=ssl.create_default_context()) as smtp:
            smtp.ehlo()
            mechanisms = smtp.esmtp_features.get('auth', '').upper().split()
            if 'PLAIN' in mechanisms:
                reply = smtp.auth('PLAIN', lambda challenge=None: '\0' + username + '\0' + password,
                          initial_response_ok=True)
            elif 'LOGIN' in mechanisms:
                # One LOGIN exchange, with no fallback to another mechanism if
                # credentials are rejected. smtplib bounds challenge count.
                smtp.user, smtp.password = username, password
                reply = smtp.auth('LOGIN', smtp.auth_login, initial_response_ok=False)
            else:
                raise EnrollmentError('The server does not advertise supported SMTP password authentication; nothing saved.')
            if reply[0] != 235:
                raise EnrollmentError('SMTP did not confirm this credential; nothing saved.')
            authenticated = True
    except smtplib.SMTPAuthenticationError:
        raise EnrollmentError('SMTP sign-in was rejected. Check the username and app password with Mail (IMAP/POP/SMTP) access; nothing saved.') from None
    except EnrollmentError:
        raise
    except Exception:
        if not authenticated:
            raise EnrollmentError('The secure SMTP verification failed; nothing saved. Check the server and network before retrying.') from None
        # QUIT cannot undo successful authentication; no email was submitted.


def _open_parent(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise EnrollmentError('Enrollment requires an absolute private configuration path.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', flags)
    try:
        for part in path.parent.parts[1:]:
            replacement = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = replacement
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_config(parent, name, owner_uid):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != owner_uid or stat.S_IMODE(metadata.st_mode) not in (0o600, 0o640)
                or metadata.st_size > MAX_CONFIG_BYTES):
            raise EnrollmentError('The configuration must be a private administrator-owned regular file; nothing saved.')
        chunks = []
        total = 0
        while True:
            block = os.read(descriptor, min(65536, MAX_CONFIG_BYTES + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > MAX_CONFIG_BYTES:
                raise EnrollmentError('The configuration exceeds the supported size; nothing saved.')
        return b''.join(chunks), metadata
    finally:
        os.close(descriptor)


@contextmanager
def configuration(path, owner_uid=0):
    """Hold a no-follow directory handle and private enrollment lock."""
    parent = _open_parent(path)
    lock = None
    try:
        directory = os.fstat(parent)
        if directory.st_uid != owner_uid or stat.S_IMODE(directory.st_mode) & 0o022:
            raise EnrollmentError('The configuration directory must be administrator-controlled; nothing saved.')
        lock = os.open('.smtp-enrollment.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        metadata = os.fstat(lock)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != owner_uid or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise EnrollmentError('The enrollment lock is not private; nothing saved.')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original, metadata = _read_config(parent, Path(path).name, owner_uid)
        yield parent, original, metadata
    finally:
        if lock is not None:
            os.close(lock)
        os.close(parent)


def save_credentials(parent, name, original, metadata, username, password):
    current, current_metadata = _read_config(parent, name, metadata.st_uid)
    identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_gid, stat.S_IMODE(item.st_mode), item.st_mtime_ns)
    if current != original or identity(current_metadata) != identity(metadata):
        raise EnrollmentError('The configuration changed during verification; nothing saved. Run enrollment again.')
    content = update_environment(original.decode('utf-8'), username, password).encode('utf-8')
    temporary = '.smtp-config-' + uuid.uuid4().hex
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        if os.fstat(descriptor).st_uid != metadata.st_uid or os.fstat(descriptor).st_gid != metadata.st_gid:
            os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError('Configuration write failed')
            offset += written
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def enroll(path=CONFIG_PATH, owner_uid=0):
    with configuration(path, owner_uid) as (parent, original, metadata):
        values = parse_environment(original.decode('utf-8'))
        default = values.get('FASTMAIL_SMTP_USERNAME') or values.get('FASTMAIL_EMAIL', '')
        username = input('SMTP sign-in email address (Enter keeps the configured account): ').strip() or default
        password = hidden_input('SMTP app password (hidden; not your account password): ')
        validate_credentials(username, password)
        host = values.get('FASTMAIL_SMTP_HOST') or 'smtp.fastmail.com'
        verify_smtp(host, username, password)
        save_credentials(parent, Path(path).name, original, metadata, username, password)


def main():
    if len(sys.argv) != 1 or os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit('Run with sudo from an interactive terminal. Do not pass credentials as arguments or pipe them in.')
    os.umask(0o077)
    print('Create a separate Fastmail app password with sending access (Mail: IMAP/POP/SMTP).\n'
          'Keep the existing IMAP app password. Enter the new password only at the hidden prompt.\n'
          'This command verifies one SMTP sign-in without sending email, saves the separate sending credential,\n'
          'and restarts the Tahor web service to load it.\n')
    try:
        enroll()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit('Enrollment cancelled; nothing saved.') from None
    except EnrollmentError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit('Enrollment could not complete. No credential details were logged; check the private configuration and permissions.') from None
    try:
        subprocess.run(['systemctl', 'restart', 'tahor-decision-app.service'], check=True, timeout=60, capture_output=True)
    except Exception:
        raise SystemExit('SMTP sign-in verified and credentials saved, but the web service did not restart. Run sudo systemctl restart tahor-decision-app.service. No email was sent.') from None
    print('SMTP sign-in verified, sending credentials saved, and the Tahor web service restarted. No email was sent.')


if __name__ == '__main__':
    main()
