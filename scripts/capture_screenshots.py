#!/usr/bin/env python3
"""Render synthetic app screenshots using an isolated headless Chrome profile."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', default=shutil.which('google-chrome') or shutil.which('chromium'))
    args = parser.parse_args()
    if not args.chrome:
        parser.error('Pass --chrome with the path to Chrome or Chromium')
    with tempfile.TemporaryDirectory(prefix='tahor-screenshots-') as directory:
        temporary = Path(directory)
        subprocess.run([sys.executable, str(ROOT / 'demo.py'), '--export', str(temporary / 'pages')], check=True)
        output = ROOT / 'docs/screenshots'
        output.mkdir(parents=True, exist_ok=True)
        for page, filename, height in [('index', 'decisions', 1080), ('unsubscribe', 'subscriptions', 1080), ('drafts', 'drafts', 850), ('settings', 'settings', 1080), ('status', 'status', 850)]:
            shot = temporary / (filename + '.png')
            process = subprocess.Popen([args.chrome, '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--hide-scrollbars', '--user-data-dir=' + str(temporary / ('profile-' + filename)), '--window-size=1120,' + str(height), '--screenshot=' + str(shot), '--virtual-time-budget=3000', (temporary / 'pages' / (page + '.html')).as_uri()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                deadline = time.monotonic() + 30
                while not shot.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                if not shot.is_file():
                    raise RuntimeError(f'Screenshot failed for {page}')
                shutil.copy2(shot, output / (filename + '.png'))
                print(f'Captured {filename}', flush=True)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


if __name__ == '__main__':
    main()
