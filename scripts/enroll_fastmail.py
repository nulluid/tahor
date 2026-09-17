#!/usr/bin/env python3
"""Admin-only hidden terminal enrollment; never accept secrets as arguments."""
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provider_connector.auth import validate_credentials, totp, CredentialError

DESTINATION = Path('/etc/credstore.encrypted/tahor-fastmail')


def hidden_input(prompt):
    # getpass otherwise falls back to echoed stdin if terminal controls fail.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SystemExit('Hidden terminal input is unavailable; nothing saved.') from None


def collect_credentials():
    def read_field(prompt, valid, message, hidden=False):
        for _ in range(3):
            value = hidden_input(prompt) if hidden else input(prompt).strip()
            try:
                accepted = valid(value)
            except CredentialError:
                accepted = False
            if accepted:
                return value
            print(message)
        raise SystemExit('Enrollment cancelled after invalid input; nothing saved.')
    try:
        username = read_field('Fastmail username: ',
            lambda value: isinstance(value, str) and 0 < len(value) <= 4096 and '@' in value and not any(c in value for c in '\r\n\0'),
            'Username is invalid. Enter the full Fastmail sign-in email address.')
        seed = read_field('New authenticator manual setup key (hidden; not the six-digit code): ',
            lambda value: bool(totp(value)),
            'Authenticator setup key is invalid or unsupported. Copy the manual setup key from the new Fastmail device, not a verification code, password, or QR address.', hidden=True)
        password = read_field('Fastmail account password (hidden; not an app password): ',
            lambda value: isinstance(value, str) and 0 < len(value) <= 4096 and '\0' not in value,
            'Account password is invalid. Enter your account sign-in password; it cannot be empty.', hidden=True)
        value = {'username': username, 'password': password, 'totp_seed': seed}
        validate_credentials(value)
        return value
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('Enrollment cancelled; nothing saved.') from None
    except CredentialError:
        raise SystemExit('Credential validation failed; nothing saved.') from None


def main():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit('Run from an interactive administrator terminal with sudo. No piped secrets.')
    print('Enroll a separate Fastmail authenticator named Tahor. Keep Bitwarden enabled.\n'
          'In Fastmail: Settings → Privacy & Security → Manage two-step verification →\n'
          'Add verification device → Authenticator app. Reveal its manual setup key.\n'
          'Credentials stay on this host; do not paste them into chat or a web app.\n'
          'This grants this isolated service full account-login authority. Root on this\n'
          'host can still access it. The stored credential is encrypted with systemd.\n')
    print('Each field is checked separately. Press Ctrl-C to cancel without saving.\n')
    value = collect_credentials()
    while True:
        print('Current verification code for the new Fastmail device: ' + totp(value['totp_seed']))
        answer = input('Save the named device in Fastmail; type saved, refresh, or cancel: ').strip()
        if answer == 'saved':
            break
        if answer != 'refresh':
            raise SystemExit('Enrollment cancelled; nothing saved.')
    DESTINATION.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if DESTINATION.parent.stat().st_mode & 0o077:
        raise SystemExit('Credential directory must be mode 0700.')
    fd, name = tempfile.mkstemp(prefix='.tahor-', dir=DESTINATION.parent)
    os.close(fd)
    try:
        result = subprocess.run(['systemd-creds', 'encrypt', '--name=fastmail', '-', name],
                                input=json.dumps(value).encode(), capture_output=True)
        if result.returncode:
            raise SystemExit('Credential encryption failed; existing enrollment preserved.')
        os.chmod(name, 0o600)
        os.replace(name, DESTINATION)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    value.clear()
    # Testing takes place under the restricted service identity, not as root.
    subprocess.run(['systemctl', 'stop', 'tahor-provider.service'], check=True)
    try:
        result = subprocess.run(['systemctl', 'start', 'tahor-provider-check.service'])
    finally:
        subprocess.run(['systemctl', 'start', 'tahor-provider.service'], check=True)
    if result.returncode:
        raise SystemExit('Credentials stored, but server sign-in did not verify. Automatic rules remain unverified.')
    print('Server sign-in verified. Enable Automatic provider rules in Tahor Settings when ready.')


if __name__ == '__main__':
    main()
