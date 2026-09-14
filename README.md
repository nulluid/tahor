# mailbox-sweep

Inbox triage that doesn't need a human: classify each message, tag it with
a retention window, delete what's aged out and already been read, and file
receipts and statements away once they've had a fair chance to be seen.

Standard library only — no dependencies to install for the scripts
themselves. Classification calls out to an OpenAI-compatible chat endpoint,
so it works equally well against a local model (LM Studio, Ollama) or a
hosted one.

## Pieces

- **classify.py** — runs each message through the model against the schema
  in `prompt.txt`: keep/trash/mixed, category, retention tier,
  business/personal, needs-attention.
- **keyword_tool.py** — writes the classifier's decision onto the message
  as real IMAP keywords, addressed by Message-ID so the same ops file is
  safe to rerun.
- **bulk_lookup.py** / **process_batch.py** — batch machinery: pull headers
  for a date range in one IMAP round trip, correlate classifier output back
  to Message-IDs, and hold back one message per sender so a fully-trashed
  sender still leaves a dated record behind.
- **retention_sweep.py** — deletes anything past its retention window, but
  only once it's actually been read.
- **filing_sweep.py** — moves receipts/statements/tax mail into per-vendor
  folders, once they've had a week (if read) or a month (if still unread)
  to be seen.

## Setup

1. `cp .env.example .env` and fill in your IMAP address and app password.
2. `cp vendor_buckets.example.json vendor_buckets.json` and
   `cp prompt.example.txt prompt.txt` — both are meant to be edited for
   your own mail and are gitignored so real values never get committed.
3. Point `classify.py` at a running OpenAI-compatible chat endpoint.
4. Per batch: `classify.py` → `process_batch.py` → `keyword_tool.py`. On a
   schedule: `retention_sweep.py` and `filing_sweep.py`.

## Design

Retention (how long to keep something) and filing (where it lives) are
separate decisions. A message can be tagged for standard retention and
still sit in the inbox indefinitely — filing out of the inbox is reserved
for the categories that have no read-now value at all. Nothing unread is
ever deleted, regardless of age, and filing itself waits out a grace period
rather than moving something before there's been a real chance to see it.
