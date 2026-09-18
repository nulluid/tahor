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
        os.environ.update(TAHOR_DB_PATH=str(state / 'decisions.db'), TAHOR_SETTINGS_PATH=str(state / 'settings.json'), TAHOR_STATUS_PATH=str(state / 'worker_status.json'), DATA_DIR=str(state), ALLOWED_EMAIL='demo@example.com', BASE_URL=f'http://localhost:{args.port}', GOOGLE_CLIENT_ID='', GOOGLE_CLIENT_SECRET='', OPENROUTER_API_KEY='', GEMINI_API_KEY='', FASTMAIL_EMAIL='', FASTMAIL_APP_PASSWORD='', TAHOR_PROVIDER_BRIDGE='', TAHOR_DATA_PUSH='0', TAHOR_NOTIFY_HEALTH='0', TAHOR_NOTIFY_DIGEST='0', TAHOR_CLASSIFY_FREE_ENABLED='1')
        sys.path.insert(0, str(ROOT / 'decision-app'))
        import app as ui
        from flask import request, session, redirect
        ui.init_db()
        (state / 'vendor_buckets.json').write_text(json.dumps({'receipts@example.net': ['Shopping/Retail', 'Example Store'], 'billing@example.org': ['Finance/Statements', 'Example Bank']}))
        ui.mailbox_settings.set_ai_task_settings('classification', 'paid_only')
        for task in ('rule', 'reply'):
            ui.mailbox_settings.set_ai_task_settings(task, 'paid_only', paid_model='grok-4.6', free_model='ling-free')
        rule_id = ui.reply_rules.save_rule(
            name='Community updates', match_type='natural_language',
            match='Updates and personal messages from community volunteers; exclude generic advertising.',
            instructions='Thank the writer for the update and mention one specific detail. Wish them well. For personal questions, address the request; leave a placeholder for details only I can supply.',
            signature='Warmly,\nAlex', max_sentences=3)
        for sender, count in [('morgan@example.com', 4), ('riley@example.org', 2), ('bulletin@example.net', 3)]:
            for index in range(count):
                ui.tahor_db.record_reply_rule_match(rule_id, f'<demo-{index}-{sender}>', sender)
        ui.reply_rules.set_sender_excluded(rule_id, 'bulletin@example.net', True)
        db = ui.tahor_db.get_db()
        now = datetime.now(timezone.utc).isoformat()
        with db:
            for kind, summary, context in [
                ('vendor_mapping', 'Choose a home for Northstar receipts', {'sender_label': 'northstar.example', 'sender_email': 'orders@northstar.example', 'routing_key': 'orders@northstar.example', 'display_name': 'Northstar Outdoors', 'suggestion_status': 'ready', 'suggestion_version': 2, 'suggestion_source': 'ai', 'suggested_action': 'review', 'suggested_bucket': 'Shopping/Retail', 'suggested_vendor': 'Northstar Outdoors', 'suggestion_reason': 'This sender mixes membership notices and purchases. Confirm the destination.', 'samples': [{'mailbox': 'INBOX', 'message_id': '<sample-receipt@example.com>', 'subject': 'Your outdoor membership and order', 'received_at': '2026-09-15T12:00:00+00:00'}]}),
                ('free_text_rule', 'Review a proposed receipt policy', {'rule_proposal': {'token': 'synthetic-preview', 'result': {'kind': 'file_edit'}, 'diff': '--- prompt.txt (current)\n+++ prompt.txt (proposed)\n@@ -1 +1 @@\n-Keep receipts for three years.\n+Keep durable equipment receipts indefinitely.\n'}}),
                ('message_review', 'Your membership renewal needs a second look', {'mailbox': 'INBOX', 'message_id': '<demo@example.com>', 'sender_email': 'membership@example.org', 'sender': 'Example Community <membership@example.org>', 'received_at': '2026-09-15T14:00:00+00:00', 'date': '2026-09-15T14:00:00+00:00', 'snippet': 'Your annual membership renewal is ready. Please review the updated terms before renewing.'}),
            ]:
                db.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES (?,?,?,'pending',?)", (kind, summary, json.dumps(context), now))
            db.execute("INSERT INTO decisions(kind,summary,context,status,resolution,created_at) VALUES (?,?,?,'pending',?,?)", (
                'free_text_rule', 'Clarify a marketing rule',
                json.dumps({'rule_clarification': {'question': 'Which exact sender domain should this rule cover? Include the full domain in your instruction; a company name alone is not enough to authorize a domain-wide rule.'}}),
                json.dumps({'action': 'free_text_rule', 'text': 'Trash marketing from Northstar Outdoors, unsubscribe, and keep purchase receipts.'}), now))
        db.close()
        for domain, name, count in [('papertrail.example', 'Papertrail Weekly', 8), ('northstar.example', 'Northstar Outdoors', 5), ('brightday.example', 'Brightday Offers', 12)]:
            ui.tahor_db.upsert_unsubscribe_candidate(domain, 'news@' + domain, name, 'https://' + domain + '/unsubscribe', None, True)
        ui.tahor_db.set_sender_rule('brightday.example', 'block_marketing')
        import business_ledger
        import expense_archive, fetch_batch
        original = b'From: Example Cloud <billing@cloud.example>\r\nMessage-ID: <demo-business-receipt@example.com>\r\nSubject: Your cloud receipt\r\nContent-Type: text/plain\r\n\r\nAmount paid: USD 24.00\nPayment date: 2026-09-15\nReceipt ID: DEMO-24'
        expense_id = business_ledger.record_receipt(dict(business_key='example-studio',matched_rule_id='software-receipts',vendor='Example Cloud',sender_email='billing@cloud.example',mailbox='Example Studio/Receipts/2026',message_id='<demo-business-receipt@example.com>',uid='1',uidvalidity='1',received_at='2026-09-15T12:00:00+00:00',subject='Your cloud receipt'), fetch_batch.extract_body_text(original), verified_business=True)
        business_ledger.update_metadata(expense_id, category='Hosting', comment='Hosted application infrastructure')
        expense_archive.store_verified(expense_id, original)
        ui.generate_sieve.refresh_sieve()
        ui.runtime_status.write_status('processed', mode='paid_only', last_batch_applied=50, last_batch_pending=0, last_success_at=now)

        @ui.app.before_request
        def preview_session():
            session['email'] = 'demo@example.com'
            if request.method == 'GET' and request.path.startswith(('/subscription-messages/', '/subscription-message/')):
                return ('<!doctype html><html><head><meta charset="utf-8"><title>Tahor — sample email</title>' + ui.STYLE_BLOCK + '</head><body><main>' + ui.tahor_header('unsubscribe') +
                        '<h1>A weekend offer from Northstar Outdoors</h1><p>From: Offers &lt;news@northstar.example&gt;</p><p>Received: September 15, 2026</p><pre style="white-space:pre-wrap">Thanks for being a customer. Use coupon code TRAIL for your next visit. Offer expires September 30, 2026.</pre><p>This is synthetic preview content. No mailbox is connected.</p><a href="/unsubscribe">Back to subscriptions</a></main></body></html>')
            if request.method != 'GET' or request.path.startswith(('/message/', '/unsubscribe-link/')):
                session['flash'] = 'This is a preview with sample data. No mailbox is connected.'
                return redirect('/')

        @ui.app.after_request
        def preview_banner(response):
            if response.mimetype == 'text/html' and not response.is_streamed:
                body = response.get_data(as_text=True).replace('<main>', '<main><p class="hint" style="text-align:right;letter-spacing:.08em">PREVIEW · SAMPLE DATA</p>', 1)
                if request.path == '/settings':
                    body = body.replace('<details><summary>Matched senders', '<details open><summary>Matched senders')
                response.set_data(body)
            return response

        if args.export:
            args.export.mkdir(parents=True, exist_ok=True)
            client = ui.app.test_client()
            for route, name in [('/', 'index'), ('/unsubscribe', 'unsubscribe'), ('/settings', 'settings'), ('/status', 'status'), ('/expenses', 'expenses')]:
                response = client.get(route)
                if response.status_code != 200:
                    raise RuntimeError(f'Preview failed: {route}')
                # Static exports have no backend; keep the actual rendered UI
                # without executing polling or mailbox action scripts.
                import re
                page = re.sub(r'<script\b[^>]*>.*?</script>', '', response.get_data(as_text=True), flags=re.S | re.I)
                (args.export / (name + '.html')).write_text(page)
            print(f'Exported synthetic preview pages to {args.export}')
        else:
            print(f'Preview: http://127.0.0.1:{args.port} (sample data; no mail is sent or changed)')
            ui.app.run(host='127.0.0.1', port=args.port, debug=False)


if __name__ == '__main__':
    main()
