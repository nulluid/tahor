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
        # A focused Settings view keeps the rule example legible in the README.
        # Render the actual controls; only unrelated sections are hidden for this shot.
        settings = (temporary / 'pages/settings.html').read_text()
        focus_style = '''<style>
main > section:not(:has(#reply-rules)), main > h1, main > form,
main > p:not(:first-child), section:has(#reply-rules) > form,
section:has(#reply-rules) > h3, section:has(#reply-rules) > p:not(:first-of-type),
section:has(#reply-rules) > details + p { display: none; }
section:has(#reply-rules) { margin-top: 0; padding-top: 0; border-top: 0; }
</style>'''
        (temporary / 'pages/reply-rules.html').write_text(settings.replace('</head>', focus_style+'</head>'))
        settings_height = 3300 + 140 * settings.count('name="reply_model"')
        output = ROOT / 'docs/screenshots'
        output.mkdir(parents=True, exist_ok=True)
        for page, filename, height in [('index', 'decisions', 1600), ('unsubscribe', 'subscriptions', 1080), ('settings', 'settings', settings_height), ('reply-rules', 'reply-rules', 1000), ('status', 'status', 850)]:
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
