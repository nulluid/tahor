#!/usr/bin/env python3
"""Admin-only hidden terminal enrollment; never accept secrets as arguments."""
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provider_connector.auth import validate_credentials, totp, CredentialError

DESTINATION = Path('/etc/credstore.encrypted/tahor-fastmail')


def main():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit('Run from an interactive administrator terminal with sudo. No piped secrets.')
    print('Enroll a separate Fastmail authenticator named Tahor. Keep Bitwarden enabled.\n'
          'In Fastmail: Settings → Privacy & Security → Manage two-step verification →\n'
          'Add verification device → Authenticator app. Reveal its manual setup key.\n'
          'Credentials stay on this host; do not paste them into chat or a web app.\n'
          'This grants this isolated service full account-login authority. Root on this\n'
          'host can still access it. The stored credential is encrypted with systemd.\n')
    value = {'username': input('Fastmail username: ').strip(),
             'password': getpass.getpass('Fastmail account password (hidden): '),
             'totp_seed': getpass.getpass('New authenticator setup key (hidden): ').strip()}
    try:
        validate_credentials(value)
    except CredentialError:
        raise SystemExit('Invalid enrollment input; nothing saved.') from None
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
