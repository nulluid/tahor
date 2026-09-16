#!/usr/bin/env python3
"""Check setup; network checks are explicit and never change mailbox messages."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-imap', action='store_true')
    parser.add_argument('--check-model', action='store_true')
    parser.add_argument('--web', action='store_true')
    args = parser.parse_args()
    required = ['FASTMAIL_EMAIL', 'FASTMAIL_APP_PASSWORD', 'OPENROUTER_API_KEY', 'DATA_DIR', 'TAHOR_DB_PATH']
    if args.web:
        required += ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'BASE_URL', 'ALLOWED_EMAIL']
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        print('Missing configuration: ' + ', '.join(missing))
        return 1
    import mailbox_settings
    import tahor_db
    tahor_db.init_db()
    mode = mailbox_settings.get_classify_mode()
    print(f'Configuration ready. Classification mode: {mode}.')
    data_dir = Path(os.environ['DATA_DIR'])
    prompt = Path(os.environ.get('PROMPT_PATH', data_dir / 'prompt.txt'))
    vendor_map = Path(os.environ.get('VENDOR_BUCKETS_PATH', data_dir / 'vendor_buckets.json'))
    for path in (prompt, vendor_map):
        if not path.is_file():
            print(f'Missing data file: {path}')
            return 1
    if args.check_imap:
        import fetch_batch
        conn = fetch_batch.connect()
        try:
            status, _ = conn.select('"INBOX"', readonly=True)
            if status != 'OK':
                raise RuntimeError('Could not select INBOX')
            capabilities = {item.decode().upper() if isinstance(item, bytes) else item.upper() for item in conn.capabilities}
            print('IMAP login and read-only INBOX access succeeded.')
            print('Filing support: ' + ('ready' if 'MOVE' in capabilities else 'MOVE missing'))
            print('Retention support: ' + ('ready' if 'UIDPLUS' in capabilities else 'UIDPLUS missing'))
        finally:
            conn.logout()
    if args.check_model:
        import classify
        name = 'openrouter-paid' if mode == 'paid' else 'openrouter-free'
        backend = classify.BACKENDS[name]
        result = classify.classify_one(backend['url'], {'Content-Type': 'application/json', 'Authorization': backend['auth_header']()}, backend['default_model'], prompt.read_text(), {'id': 'setup-check', 'subject': 'Hello', 'from': 'sample@example.com', 'snippet': 'A synthetic setup test; no mailbox data.'}, retries=0)
        if result['action'] == 'error':
            print('Model check failed: ' + result['reason'])
            return 1
        print(f'Model responded successfully ({name}). No mailbox messages changed.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Check failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
