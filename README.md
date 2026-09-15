# Tahor

Tahor (Hebrew טָהוֹר, "clean, pure") is a headless mailbox-triage pipeline.
An LLM classifies each message, tags it with real IMAP keywords, and
separate sweeps delete or file mail based on that tag. For the cases the
classifier can't call confidently, an optional small web app lets a human
resolve them asynchronously instead of blocking the pipeline. Everything
here is standard-library-only Python except the web app, which needs
Flask and requests.

## Architecture

- **Always running (pick one):** `backlog_worker.py`, an unattended loop
  under systemd that fetches, classifies, tags, and repeats -- or run
  `fetch_batch.py` / `classify.py` / `process_batch.py` / `keyword_tool.py`
  by hand or from cron, one stage at a time.
- **Sweeps (scheduled, separate from classification):** `retention_sweep.py`
  deletes mail whose retention window has passed and that's been read;
  `filing_sweep.py` moves receipts and statements into per-vendor folders
  once they've had a fair chance to be seen.
- **Decision app (optional):** `decision-app/app.py`, a small Flask site
  for resolving ambiguous cases -- new vendor mappings, free-text rules,
  unsubscribe candidates, drafted replies -- from a browser instead of a
  live session.
- **Reply drafting (optional):** `draft_replies.py` watches for mail from
  senders you've configured as reply triggers (settings page) and drafts
  a reply for each -- saved into your Drafts folder as a real in-thread
  reply, never sent automatically.

## Requirements

- Python 3.9+.
- An IMAP account with app-password support. This was built against
  Fastmail; any IMAP host works.
- An OpenAI-compatible chat endpoint: a local model server (LM Studio,
  Ollama) or a hosted API key (Gemini, OpenRouter, or anything else that
  speaks the same protocol).

## Quick start

```
git clone <this repo>
cd tahor
./install.sh
```

Then:

1. Edit `.env` with your IMAP host, address, and app password.
2. Edit `vendor_buckets.json` and `prompt.txt` for your own mail --
   they start from generic examples and are gitignored so real values
   never get committed.
3. Pick a `CLASSIFY_BACKEND` (see `classify.py`'s docstring for the
   tradeoffs between local/gemini/openrouter/openrouter-free).
4. Run a batch by hand before automating anything, so you can see what
   it actually does to a real mailbox:
   ```
   python3 fetch_batch.py INBOX current_batch
   python3 classify.py current_batch_in.json current_batch_out.json prompt.txt
   python3 process_batch.py current_batch INBOX
   python3 keyword_tool.py current_batch_ops.json
   ```
5. Once that looks right, set up ongoing operation. Either the always-on
   worker:
   ```
   sudo cp tahor-backlog-worker.service.example /etc/systemd/system/tahor-backlog-worker.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now tahor-backlog-worker
   ```
   or cron, running each stage on its own schedule:
   ```
   */30 * * * * cd /path/to/tahor && python3 fetch_batch.py INBOX current_batch && python3 classify.py current_batch_in.json current_batch_out.json prompt.txt && python3 process_batch.py current_batch INBOX && python3 keyword_tool.py current_batch_ops.json
   0 4 * * * cd /path/to/tahor && python3 retention_sweep.py
   0 5 * * * cd /path/to/tahor && python3 filing_sweep.py
   */30 * * * * cd /path/to/tahor && python3 draft_replies.py
   ```

## The decision app

Some cases aren't clear-cut: a sender you haven't mapped to a folder yet,
or a rule you want to state in your own words rather than as JSON. The
decision app queues those up on a page instead of blocking the pipeline,
and `apply_decisions.py` picks up what you resolved on the next run.

It has no built-in auth beyond Google OAuth restricted to one address, so
set that up first:

1. In the Google Cloud Console, create an OAuth client ID (Web
   application). Set the authorized redirect URI to
   `<BASE_URL>/auth/google/callback` -- for a local-only setup that's
   `http://localhost:8420/auth/google/callback`.
2. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BASE_URL`, and
   `ALLOWED_EMAIL` (the one address allowed to sign in) in `.env`.
3. Run it: `python3 decision-app/app.py`.

It binds to `127.0.0.1` only. Don't expose it to the internet without
putting TLS in front of it yourself -- nginx with certbot, Caddy, or a
tunnel. An unauthenticated decision queue on the open internet is a bad
idea regardless of the OAuth check.

### Unsubscribing and blocking senders

Any sender whose mail carries a `List-Unsubscribe` header shows up on the
`/unsubscribe` page. Four choices per sender: unsubscribe only, unsubscribe
and block that sender's marketing mail going forward (transactional mail --
receipts, shipping notices -- still comes through), block everything from
that sender outright, or leave the subscription alone. A one-click
(RFC 8058) unsubscribe fires as a real HTTP request; anything else is a
best-effort attempt (a plain link click or an unsubscribe email) with no
guarantee it's honored, which is exactly why blocking exists alongside it.

### Reply drafting

Add a trigger on the settings page -- a specific address or a whole
domain -- and matching mail gets a drafted reply from whichever model
you've picked for rule drafting. The draft lands in your Drafts folder as
a real in-thread reply (`In-Reply-To`/`References` set, so it threads
correctly) and is never sent on its own; it also shows up on `/drafts` for
a second look. Each run that produces at least one draft sends you a
one-line summary email so a new draft is never silently missed.

## Deploying on Oracle Cloud's Always Free tier

An Always Free Ampere A1 instance runs this comfortably -- it's a small,
low-traffic workload. The A1 shape is ARM, so if you add any dependency
beyond Flask and requests, check it ships an ARM wheel or is pure Python;
both of Tahor's own dependencies already do.

Nothing here is actually Oracle-specific. A Raspberry Pi, a $5 VPS, or a
Fly.io machine works the same way -- Oracle is just where this happened to
get built.

## Why these design choices

Retention and filing are separate decisions. A message can be tagged for
standard retention and still sit in the inbox indefinitely -- filing out
of the inbox is reserved for categories with no read-now value at all
(receipts, statements, tax mail). Keeping them separate means a filing
mistake never risks a deletion, and a retention tier never determines
where something ends up living.

Retention has four tiers: transient (days -- OTPs, duplicate alerts),
standard (years -- the default for ordinary keeps), forever (used
sparingly -- legal documents, durable-goods receipts), and
pending-review (genuinely ambiguous, left alone rather than guessed at).

Nothing unread is ever deleted, regardless of age or tier. Filing itself
waits out a grace period too, rather than moving something out of the
inbox before there's been a real chance to see it.

## License

MIT — see LICENSE.
