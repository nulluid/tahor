#!/usr/bin/env python3
"""Exercise fresh installation and rerun safety in a disposable checkout."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='tahor-install-check-') as directory:
        workspace = Path(directory)
        checkout = workspace / 'tahor'
        shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns('.git', 'venv', '.venv', '__pycache__', '.env', '.flask_secret_key*', '*.db*', '*.lock', 'prompt.txt', 'vendor_buckets.json', 'sieve.txt', 'current_batch*', 'processed_message_ids.txt', 'settings.json', 'worker_status.json'))
        config = workspace / 'instance'
        env = dict(os.environ, FASTMAIL_APP_PASSWORD='synthetic-password', OPENROUTER_API_KEY='synthetic-key')
        command = ['bash', 'install.sh', '--non-interactive', '--email', 'demo@example.com', '--config-dir', str(config), '--systemd-dir', str(workspace / 'units')]
        subprocess.run(command, cwd=checkout, env=env, check=True)
        (config / 'data/prompt.txt').write_text('A private custom prompt.\n')
        subprocess.run(command, cwd=checkout, env=env, check=True)
        assert (config / 'data/prompt.txt').read_text() == 'A private custom prompt.\n'
        python = str(checkout / 'venv/bin/python')
        subprocess.run([python, 'run.py', '--env', str(config / 'config.env'), 'doctor'], cwd=checkout, check=True)
        subprocess.run([python, '-m', 'pip', 'install', '--quiet', '-r', 'requirements-dev.txt'], cwd=checkout, check=True)
        subprocess.run([python, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=checkout, check=True)
        subprocess.run([python, 'demo.py', '--export', str(workspace / 'preview')], cwd=checkout, check=True)
        print('Fresh install, repeated install, doctor, regression tests, and preview export passed.')


if __name__ == '__main__':
    main()
