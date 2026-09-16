#!/usr/bin/env python3
"""Preview the real app with synthetic data and no mailbox or model access."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8421)
    parser.add_argument('--export', type=Path, help='Export static pages instead of starting a preview server')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tahor-demo-') as directory:
        state = Path(directory)
        os.environ.update(TAHOR_DB_PATH=str(state / 'decisions.db'), TAHOR_SETTINGS_PATH=str(state / 'settings.json'), TAHOR_STATUS_PATH=str(state / 'worker_status.json'), DATA_DIR=str(state), ALLOWED_EMAIL='demo@example.com', BASE_URL=f'http://localhost:{args.port}', GOOGLE_CLIENT_ID='', GOOGLE_CLIENT_SECRET='', OPENROUTER_API_KEY='', GEMINI_API_KEY='', FASTMAIL_EMAIL='', FASTMAIL_APP_PASSWORD='')
        sys.path.insert(0, str(ROOT / 'decision-app'))
        import app as ui
        from flask import request, session, redirect
        ui.init_db()
        ui.mailbox_settings.set_classify_mode('free')
        ui.mailbox_settings.add_reply_trigger('sender_email', 'morgan@example.com')
        db = ui.tahor_db.get_db()
        now = datetime.now(timezone.utc).isoformat()
        with db:
            for kind, summary, context in [
                ('vendor_mapping', 'Choose a home for Northstar receipts', {'sender_label': 'northstar.example', 'note': 'Keep receipts together in a folder you choose.'}),
                ('message_review', 'Your membership renewal needs a second look', {'mailbox': 'INBOX', 'message_id': '<demo@example.com>', 'note': 'An ambiguous message stays protected until you decide.'}),
            ]:
                db.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES (?,?,?,'pending',?)", (kind, summary, json.dumps(context), now))
        db.close()
        for domain, name, count in [('papertrail.example', 'Papertrail Weekly', 8), ('northstar.example', 'Northstar Outdoors', 5), ('brightday.example', 'Brightday Offers', 12)]:
            ui.tahor_db.upsert_unsubscribe_candidate(domain, 'news@' + domain, name, 'https://' + domain + '/unsubscribe', None, True)
        ui.tahor_db.set_sender_rule('brightday.example', 'block_marketing')
        ui.generate_sieve.refresh_sieve()
        ui.tahor_db.create_reply_draft('<demo-message@example.com>', '<demo-thread@example.com>', 'morgan@example.com', 'Coffee next Thursday?', 'Thursday works for me. Would 10:30 suit you? I can meet at the cafe near your office.', 'morgan@example.com')
        ui.runtime_status.write_status('processed', mode='free', last_batch_applied=50, last_batch_pending=0, last_success_at=now)

        @ui.app.before_request
        def preview_session():
            session['email'] = 'demo@example.com'
            if request.method != 'GET':
                session['flash'] = 'This is a preview with sample data. No mailbox is connected.'
                return redirect('/')

        @ui.app.after_request
        def preview_banner(response):
            if response.mimetype == 'text/html' and not response.is_streamed:
                body = response.get_data(as_text=True).replace('<main>', '<main><p class="hint" style="text-align:right;letter-spacing:.08em">PREVIEW · SAMPLE DATA</p>', 1)
                response.set_data(body)
            return response

        if args.export:
            args.export.mkdir(parents=True, exist_ok=True)
            client = ui.app.test_client()
            for route, name in [('/', 'index'), ('/unsubscribe', 'unsubscribe'), ('/drafts', 'drafts'), ('/settings', 'settings'), ('/status', 'status')]:
                response = client.get(route)
                if response.status_code != 200:
                    raise RuntimeError(f'Preview failed: {route}')
                (args.export / (name + '.html')).write_text(response.get_data(as_text=True))
            print(f'Exported synthetic preview pages to {args.export}')
        else:
            print(f'Preview: http://127.0.0.1:{args.port} (sample data; no mail is sent or changed)')
            ui.app.run(host='127.0.0.1', port=args.port, debug=False)


if __name__ == '__main__':
    main()
