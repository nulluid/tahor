<p align="center">
  <img src="docs/assets/hero.svg" alt="Tahor — a calmer inbox, on your terms" width="100%">
</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="#inside-the-app">See the app</a> ·
  <a href="#how-it-works">Architecture</a> ·
  <a href="docs/setup.md">Setup guide</a> ·
  <a href="docs/operations.md">Operations</a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-2bc7bb?style=flat-square"></a>
  <img alt="Python 3.9 and newer" src="https://img.shields.io/badge/Python-3.9%2B-10302c?style=flat-square">
  <img alt="Self hosted" src="https://img.shields.io/badge/self--hosted-your%20mailbox-10302c?style=flat-square">
</p>

**Tahor is a self-hosted email assistant that works inside your existing mailbox.**
It starts with your inbox, works through the backlog without a folder-size limit,
tags messages, files receipts, helps you unsubscribe,
and prepares replies for review. Your usual email client stays your email client.

The name comes from **טָהוֹר**, Hebrew for “clean, pure.” The aim is practical:
less inbox maintenance, fewer lost receipts, and a clear place to review decisions
that should not be left to a model.

| The routine work | Your control |
| :--- | :--- |
| Classify and tag messages in the background | Review ambiguous mail before retention cleanup |
| File routine receipts and statements, then mark them read | Choose the vendor’s destination in the app |
| Track unsubscribe requests and sender blocks | Keep subscriptions, block marketing, or block a domain |
| Draft a response when your reply rule matches | Find the original unread; edit and send its draft in your mail client |
| Recover from provider failures | See actual worker progress and retry status |
| Place optional health alerts and daily summaries in your inbox | Choose notifications and digest time; summaries link to Tahor and expire automatically |

## Inside the app

<p align="center">
  <img src="docs/screenshots/decisions.png" width="100%" alt="Tahor’s decision queue with routing choices, rule proposals, and clarification from observed senders">
</p>
<p align="center"><sub>The decision queue keeps unresolved choices visible. Screenshots use synthetic data.</sub></p>

<table>
<tr>
<td width="50%"><img src="docs/screenshots/subscriptions.png" alt="Unsubscribe actions and editable sender blocks"><br><strong>Subscriptions, with an exit.</strong><br>Request an unsubscribe, keep transactional mail, and remove a block later.</td>
<td width="50%"><img src="docs/screenshots/reply-rules.png" alt="Natural-language reply instructions and sender opt-outs in Settings"><br><strong>Your instructions, your mailbox.</strong><br>Describe which messages deserve a reply and how it should read. Review the draft in your usual email client.</td>
</tr>
</table>

The app also includes separate model settings for classification, rule drafting,
and reply drafting, plus a Status page showing the last successful batch.
[Try the sample-data preview](#try-it-without-connecting-a-mailbox).

<details>
<summary>See model controls and live worker status</summary>

| Choose models and inbox timing | See whether work is progressing |
| :---: | :---: |
| ![Model settings and configurable read/unread inbox timing](docs/screenshots/settings.png) | ![Worker progress and last successful batch](docs/screenshots/status.png) |

</details>

## Get started

You need **Python 3.9+**, an IMAP mailbox with an app password, and an
[OpenRouter API key](https://openrouter.ai/keys). Fastmail is the reference
provider. Other providers need IMAP keywords; filing requires `MOVE`, and
retention requires `UIDPLUS`. The setup check reports these capabilities.

```bash
git clone https://github.com/nulluid/tahor.git
cd tahor
./install.sh --mode paid_only
```

The installer creates a virtual environment, asks for credentials without
echoing them, and writes private configuration outside the checkout. New setups
leave AI rule and reply writing **disabled until you select a model in Settings**. Rerunning setup preserves your existing
configuration, prompt, and routing rules.

This command explicitly selects always-paid classification. Without `--mode paid_only`,
a new installation uses always-free classification. Read the free-model limitations
below before processing your mailbox; free mode never silently incurs paid charges.

Check the connection, then start processing:

```bash
venv/bin/python run.py doctor --check-imap --check-model
venv/bin/python run.py worker
```

The check uses a read-only mailbox connection and a synthetic model request.
The worker applies classification tags and deletes messages classified as trash.
Folder filing and age-based retention cleanup run as separate scheduled jobs.

**For the complete setup:** [web login, unattended services, safe sweep previews,
and HTTPS](docs/setup.md). Google OAuth and your mailbox/provider credentials
must be configured by you; the installer does not create those accounts.

### Try it without connecting a mailbox

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python demo.py
```

Open **http://127.0.0.1:8421**. This runs the real UI with disposable sample data.
It cannot send mail, change a mailbox, or make model requests. No account or API
key is needed.

## Replies where you already read mail

Describe a rule in **Settings → Reply rules**: which messages should match, what
the reply should say, a signature, and a limit of one to three body sentences.
Use natural language or match a specific sender or domain. For example, ask for
brief acknowledgments of community project updates that mention a specific
milestone, while answering personal questions according to their actual content.
Expand the rule’s matched-sender list to opt individual senders out.

Tahor saves a threaded reply in your mailbox’s **Drafts** folder and leaves the
original unread. A separate check with your selected writing model reviews the
reply against the source and your instructions; a rejected reply is revised once
and checked again. Rejected text is not added to Drafts. Model checks can still
miss mistakes; review every reply before sending. Personal questions
that need your answer also receive an attention flag. Edit and send it in your
usual email client; there is no separate
web draft editor and **Tahor never sends these replies automatically**. Choose a
separate spending policy for reply writing in Settings. Writer and verifier use
the same model for each attempt. Provider failures retry after a five-minute
cooldown, using another tier only when your policy permits it. Quality checks
can leave a draft pending even when a model is available.
**Ling 3.0 Flash VL** is the free writing option, restricted to Novita with zero
data retention, data collection denied, and zero input/output pricing. It can
invent promises or details; review every draft. These
[routing controls](https://openrouter.ai/docs/guides/features/zdr) protect provider
selection; they do not guarantee the accuracy of a draft.
All rule and reply writing requests require zero data retention and denied data
collection. Missing or unsupported selections never silently switch to a paid model. A reply
address must pass syntax and DNS checks. Those checks cannot prove that the
recipient’s mailbox accepts delivery.

New rules can scan recent inbox mail as well as new arrivals. The usual inbox
window still applies: three days for read mail, seven for unread by default.
Creating a draft does not extend that window. An optional filing destination on
the rule lets eligible low-attention messages move into your folder structure
afterward. Messages needing attention or review stay protected.

Natural-language matching shares the existing classification request, using up
to 6,000 characters of message context when such rules are enabled. Uncertain
matches remain protected from deletion and do not produce a draft; uncertainty
about drafting alone does not create a keep-or-trash decision. Reply writing is a
separate model request. Choose its model and spending policy independently of
classification: **Grok 4.6** is the recommended paid writer, and **Ling 3.0 Flash VL**
is the free option. Additional supported choices are available in Settings.
[Model routing and lower-cost writing options](docs/operations.md).

## Decisions without the busywork

Tahor uses observed messages to identify merchants and automatically route confident
receipt and statement matches. Shared delivery services are matched by the actual
sender address; a marketplace purchase does not turn every future receipt into the
same product category. Uncertain cases show the sender, date, suggested folder,
and a link to read the email safely before deciding. Actions update the queue in
place so you can keep working without losing your position.

**Keep** releases a review hold and preserves normal retention and filing rules.
**Keep briefly** expires the message after the configured three-day read or
seven-day unread window, measured from delivery. **Trash** deletes that message;
**Skip for now** leaves it protected and pauses a saved decision’s retry.

For subscriptions, **Stop marketing, keep transactions** requests an unsubscribe
and blocks future marketing in Tahor while allowing receipts and payment notices.
**View emails** opens recent subjects, dates, and full message text in a modal
without leaving the page or marking mail read. The same preview works on review
cards. Choose radio options for multiple senders, then **Apply selected actions**.
The action bar stays visible as you scroll, and each sender shows its own result.
**Generate AI suggestions** selects recommendations for your review using past
decisions and your private guidance; it never applies them. Each batch covers
50 senders by default, configurable in Settings. Some senders require confirmation on their website;
email-based requests require an app password with SMTP sending access.

## Choose the pace

Choose independently for **classification, reply drafting, rule drafting, and
subscription recommendations**. Subscription recommendations default to paid
with free fallback.
Settings save as you change them, with a confirmation beside each section:

| Policy | Behavior | Paid requests |
| :--- | :--- | :--- |
| **Always paid** | Retry paid failures; notify you if intervention is needed | Yes; never falls back to free |
| **Paid with free fallback** | Use free only while paid is failing; periodically probe paid recovery | Normally |
| **Free with paid escalation** | Start free; use paid if the estimated queue exceeds four hours or free temporarily fails; return to free | When needed |
| **Always free** | Keep retrying free failures; never switch to paid | Never |

Fully successful paid-only batches continue immediately, without the free-tier
pause. Speed mode controls routing and concurrency; it does not change your
classification instructions or mailbox safety checks.
The worker logs fetch, classification, and application timings for tuning.
Paid classification uses **Gemini 3.8 Flash through Google Vertex**, restricted
to a zero-retention endpoint with data collection denied and provider fallback
disabled. It allows **eight concurrent requests**, with request starts spaced
**three seconds apart**, including retries. Both settings are configurable for your
provider limits. Concurrency overlaps slow responses; pacing limits request volume.

**Inbox first.** While the Inbox has work, it receives three batches for every
ordinary-folder batch. Trash waits until the Inbox is caught up. A separate,
read-only discovery connection finds work and new folders without repeatedly
opening processing connections to empty folders. New Inbox mail is checked between
batches; an in-flight batch finishes before the next folder is selected.

For an internet-facing server, use the [dedicated service-account setup](docs/service-isolation.md)
to keep application code read-only and run without administrator privileges.

Policy changes are checked before each new classifier backend stage, including
fallbacks and recovery probes. Requests already submitted may finish; remaining
work stays queued if its policy changes. Failures leave work pending. Classifier
recovery deadlines survive worker restarts; tier probes follow a five-minute
cooldown, and persistent problems trigger the configured health alerts. The
four-hour threshold is an estimate based on queued work and observed free
throughput, not a completion guarantee. `TAHOR_CLASSIFY_FREE_ENABLED=0` remains
an optional operator override that blocks free classification requests.

Daily summaries link to your Tahor instance using its configured `BASE_URL`.
Tahor automatically deletes its own verified digests after the configured inbox
window: three days for read summaries and seven for unread by default. Health
alerts retain their normal retention policy. See [notifications](docs/notifications.md).

AI-generated rule changes appear as proposals in the decision queue. Review the
exact sender action or file diff before approving. Domain blocks require the exact
domain in your instruction; a brand name or single email address cannot authorize
a whole-domain block. If the scope is unclear, Tahor preserves your instruction
and asks for clarification instead of guessing or repeatedly retrying. Edit the
complete instruction in the decision queue, optionally choose a sender domain
Tahor has observed in your mailbox, and resubmit it for a new proposal.
Changed underlying rules invalidate an older proposal.
**Grok 4.6** is the recommended rule-writing option, selected independently from
classification and reply writing; its requests use the xAI zero-retention route.

### A setup without an added inference bill

Choose **Always free** for each enabled AI task and use an existing computer for
hosting. Rule and reply writing remain disabled until you enable them in Settings.
The free model uses the same privacy restrictions as the paid routes, but has
known accuracy limitations: it can classify important mail as trash, choose the
wrong destination for a rule, or add unsupported promises and details to a reply.
Since classification can trigger deletion, read the disclosures in Settings before
enabling free processing. Generated rules require approval; replies remain drafts
for you to review. Paying for a model does not guarantee correctness either.

This does not make your email account, hardware, electricity, or hosting free.
Free model capacity and account quotas are provider-controlled. OpenRouter can
also reject free requests when an account balance is negative.
[Provider limits](https://openrouter.ai/docs/api_reference/limits) ·
[Free model variants](https://openrouter.ai/docs/guides/routing/model-variants/free)

## How it works

```mermaid
flowchart LR
    Inbox[(IMAP mailbox)] --> Fetch[Fetch unclassified UIDs]
    Fetch --> Model[Classify]
    Model --> Tags[Apply IMAP keywords]
    Tags --> Review[Human review]
    Tags --> Filing[Scheduled filing]
    Tags --> Retention[Scheduled retention]
    Review --> Rules[Routing and sender rules]
    Rules --> Model
    Filing --> Inbox
    Retention --> Inbox
```

The worker searches for unclassified UIDs instead of repeatedly downloading the
whole inbox. A message is recorded as processed only after its IMAP keyword
write succeeds. Failed writes and classifications remain eligible for retry.
Messages without a Message-ID use a local identity derived from the mailbox and
UID. Before applying saved UID operations, Tahor checks the mailbox’s UIDVALIDITY
value so a reset cannot redirect an old operation to a different message.

**Filing and retention have their own scheduled sweeps.** You can preview both
before enabling them. The worker applies classifications immediately and deletes
explicit trash after its tags are saved. Failed deletions retain a retry marker.
The workers use IMAP and HTTP directly; reply-address validation adds a small
DNS-aware validator. The optional web app uses Flask, requests, and Gunicorn. SQLite holds review decisions and draft state. No broker,
external database, or frontend build is required.

| Component | Responsibility |
| :--- | :--- |
| `backlog_worker.py` / `mailbox_scheduler.py` | Prioritize Inbox work, discover active folders, classify, and retry failed batches |
| `process_batch.py` / `keyword_tool.py` | Enforce sender rules and track confirmed mailbox writes |
| `filing_sweep.py` | Move aged receipts, statements, and tax mail with IMAP `MOVE` |
| `retention_sweep.py` | Delete expired mail and retry pending trash deletion with targeted UID expunge |
| `decision-app/` | Review decisions, apply rules, manage subscriptions and model settings |
| `draft_replies.py` / `reply_rules.py` | Match owner instructions and prepare recoverable, thread-aware mailbox drafts |
| `runtime_status.py` / `notifications.py` | Share worker progress; optionally add health alerts and daily counts to the owner’s inbox |
| `scripts/private_backup.py` / `scripts/offhost_backup.py` | Verify private snapshots, keep off-host recovery copies, and restore after stopping services |
| `setup_tahor.py` / `run.py` | Configure a private instance and launch each component consistently |

### Engineering choices

| Decision | Reason |
| :--- | :--- |
| Mailbox keywords are the processing checkpoint | A failed write leaves the message eligible for retry, even if a local audit file says it was seen |
| Persist a draft before appending it to IMAP | A retry reuses the prepared text and checks its stable Message-ID before saving again |
| Lock settings updates and replace files atomically | Concurrent web and worker updates preserve each other’s fields |
| Separate public code, private rules, and runtime state | Instances can share a release without sharing credentials or mailbox data |

These behaviors have [regression tests](tests). The tradeoff is a deliberately
small deployment: one mailbox owner and one host, with SQLite and local file locks.

## Boundaries that matter

- **Give mail time in the inbox.** Folder moves wait three days for read mail and
  seven days for unread mail by default. Change both in Settings. Categorization
  happens immediately; these delays apply only to filing.
- **Filed mail should not clutter unread counts.** After a successful folder move,
  eligible mail is marked read. The same check cleans up qualifying unread mail
  already in existing folders, including on a new installation. Messages needing
  attention or review, starred messages, and recent unread mail are left alone.
- **Trash does not wait to be read.** Messages classified as trash are tagged and
  deleted immediately. Other mail is deleted when its retention period expires,
  whether read or unread. Forever, pending-review, and needs-attention tags protect
  messages from retention cleanup.
- **Reply drafts are never sent automatically.** A stable draft Message-ID lets
  retries recover an interrupted save without intentionally appending another copy.
  The original stays unread so the conversation remains visible.
- **Uncertain mail stays reviewable.** Ambiguous classifications receive a
  pending-review retention tag and a decision in the app.
- **Notifications are optional.** Health alerts and daily summaries are written directly
  into your inbox over IMAP, addressed from you to yourself. They contain status
  and counts, not message content; no SMTP permission is needed.
  [Enable notifications](docs/notifications.md).
- **Failures stay visible.** A failed rule can be retried; a failed unsubscribe is
  not silently marked successful. A saved block still applies if unsubscribing fails.
- **Provider rules are your choice.** Review and install generated Sieve yourself,
  or opt into the [isolated Fastmail connector](docs/fastmail-connector.md) for automatic
  whole-domain blocks. The experimental connector supports fresh password/TOTP sign-in,
  keeps credentials out of the web app, and manages only its own rule IDs. Login and
  settings authorization have separate, persistent retry safeguards. Enrollment
  grants it full account-login authority; Fastmail's unpublished interface can change.
- **Hosted models receive mail content.** Classification sends sender, subject,
  date, and a body excerpt (up to 6,000 characters with natural-language reply
  rules). Reply drafting and verification send a longer excerpt, your mailbox identity,
  signature, and writing instructions. Rule
  drafting sends the instruction and current routing/prompt configuration.

Retention defaults are seven days for transient mail and three years for standard
mail. The reference sweeper excludes Trash, Spam, Sent, Drafts, and Archive.
Review [configuration and operating limits](docs/operations.md) before enabling it.

## Your data stays separate

```text
~/.config/tahor/
├── config.env          # credentials and instance settings; mode 0600
├── data/
│   ├── prompt.txt
│   ├── vendor_buckets.json
│   └── sieve.txt
└── state/
    ├── decisions.db
    ├── settings.json
    ├── worker_status.json
    └── notifications.json # optional private delivery ledger
```

The public repository contains reusable code, tests, example configuration, and
sample-data screenshots. Real prompts, vendor mappings, and Sieve rules can live
in a separate **private** repository through `DATA_DIR`. Data commits stay local
unless you explicitly enable pushing. Credentials, message batches, databases,
and logs do not belong in either repository. Gitignore rules help; review staged
changes before publishing.

Private snapshots preserve your rules, preferences, draft journal, and pending
decisions. A separate computer can pull and verify recovery copies over SSH,
including the Fastmail connector’s rule-ownership records. Login credentials and
session tokens are excluded. Enable overdue-backup alerts to detect a missed copy.

[Set up backups and recovery](docs/private-backups.md) ·
[Health alerts and daily summaries](docs/notifications.md)

## Development and verification

```bash
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m unittest discover -s tests -v
```

Tests exercise behavior across provider recovery, confirmed IMAP writes, retention
safety, concurrent settings updates, web actions, OAuth/CSRF checks, Sieve syntax,
reply-draft recovery, and repeatable setup. They use disposable state and fake
network boundaries, without connecting to a real mailbox.

The sample preview and screenshots are reproducible:

```bash
venv/bin/python demo.py --export /tmp/tahor-preview
venv/bin/python scripts/capture_screenshots.py --chrome /path/to/chrome
```

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) ·
[Setup](docs/setup.md) · [Operations](docs/operations.md)

---

MIT licensed. Built to make a mailbox easier to live with.
