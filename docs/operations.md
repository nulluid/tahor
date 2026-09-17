# Operating Tahor

[← Tahor](../README.md) · [Setup](setup.md)

## Know what is running

The continuous worker classifies and tags every selectable folder, with no size limit.
It checks INBOX between other folders and rediscovers folders on each pass. It deletes explicit trash immediately but does not run age-based retention or
filing itself. Reply drafting has its own watcher. The web app reads the same
SQLite database and settings file; its Status page reports the worker’s actual
last batch rather than inferring health from the selected speed.

```bash
venv/bin/python run.py status
venv/bin/python run.py status --json
venv/bin/python run.py status --check
```

`--check` returns a failure exit code if status is absent, more than thirty minutes
old, or currently reporting an error/retry. It is a lightweight health signal,
not proof that every service or provider is available. The minimal web endpoint
`/healthz` checks app/database connectivity without exposing mailbox details.

With the provided user units:

```bash
journalctl --user -u tahor-backlog-worker -f
journalctl --user -u tahor-web -n 50 --no-pager
journalctl --user -u tahor-draft-replies -n 50 --no-pager
systemctl --user list-timers 'tahor-*'
```

## Mailbox reply rules

Reply rules live in Settings, with matching instructions separated from writing
instructions. Choose natural-language matching or an explicit sender/domain;
set a signature, one to three body sentences, and optionally a filing folder.
The matched-sender list provides per-rule opt-outs. Existing sender triggers
remain supported.

Semantic matching reuses the main classification call with up to 6,000 characters
of message context. A bounded recent-inbox scan covers already-classified mail
when rules change. Definite matches can receive drafts; uncertain matches are
held for review. A positive match protects the message from automatic trash
classification. Ordinary read/unread inbox timing still determines whether it
is eligible for a new draft or routine filing; preparing a draft does not reset
its age. Attention and review holds continue to prevent filing.

The draft watcher saves a reply to the regular mailbox’s Drafts folder, with
thread headers, and leaves the original unread. It does not send mail. Review and
send in your normal email client. A present Reply-To must validate; From is used
only when Reply-To is absent. Syntax and DNS checks cannot confirm
that a specific remote mailbox accepts replies. Unsupported or unsafe reply
addresses do not result in a draft.

AI writing starts disabled until you choose a model in Settings. You can select
a private writing route independently of classification speed. Treat generated text as
a draft: check factual claims, requested commitments, and the recipient before
sending. A prompt cannot make unavailable personal facts known to the model.

Before accepting new text, Tahor makes a separate verification call using the same
configured reply model, source excerpt, and private owner directions. It checks
factual grounding, required details, unsupported promises, and handling of personal
questions. A rejected reply is regenerated once with review feedback and checked
again; invalid or rejected results remain retryable and are not appended. The
signature is attached after verification. This adds model calls and improves
checks; it is not a guarantee that model-written prose is correct.

**GPT-5.1 Flex** is an optional reply-writing choice. Both generation and verification
use OpenRouter's Flex endpoint, with reasoning disabled and JSON output requested.
The server requires the response to confirm the Flex tier; it preserves pending
work on capacity errors or an unexpected tier. There is no automatic upgrade to
standard-price GPT-5.1. The published Flex token rates are half the corresponding
standard rates; slower or unavailable capacity is possible. This uses ordinary
requests, not the asynchronous Batch API or its 24-hour queue. See
[OpenRouter service tiers](https://openrouter.ai/docs/guides/features/service-tiers).
Changing the reply model does not change classification or rule-drafting models,
and fresh installations leave both writing features disabled until selected.

The verifier also identifies personal questions or requests needing the owner's
answer. Those messages receive `needs-attention` before the draft is saved, and
that decision persists across append retries. A draft containing an owner-fillable
`[please add ...]` placeholder also receives that flag even if the model misses
the need for attention. Routine newsletters do not receive
this extra hold, so ordinary inbox timing still applies. Cached text prepared
before verification was introduced is regenerated and checked before any new
append; an already existing draft is reconciled without being rewritten.

### Independent AI policies and automatic recovery

Settings provides Always paid (`paid_only`), Paid with free fallback (`paid`),
Free with paid escalation (`auto`), and Always free (`free`) separately for
classification, reply writing and rule writing. Always free never calls paid;
always paid never calls free. Auto starts free and may use paid for a queue
estimated to exceed four hours or a temporary free-provider failure. Cooldowns
last five minutes before another recovery probe; estimates are not guarantees.
A running classification batch retains its starting policy. Writing rechecks
policy before changing tiers; an HTTP call already sent cannot be recalled.
Writing starts disabled until a model is selected. Its paid/free model choices
are independent of classifier models, and writer/verifier share one model per attempt.

All hosted requests enforce `provider.zdr: true` and `provider.data_collection: deny`,
including retries and verification. The selected Ling free route is pinned to
Novita with provider fallback disabled and zero-price limits. Direct-provider
writing routes with unverified account privacy remain blocked.

Writing recovery state and per-item failure timestamps persist privately in
`ai_routing_state.json` next to the database. Failures lasting at least thirty
minutes appear in configured health alerts; temporary failures that recover do
not notify. Resolved rule requests retry every five minutes through
`tahor-decisions.timer`. Proposals awaiting approval are not regenerated.

Content rejection is distinct from a provider outage: unsupported facts or a
failed instruction check never become a successful draft just because fallback
is available. If both providers fail, the source remains available for retry and
an empty `preparing` journal entry makes the pending work visible to daily
summaries. The journal is created before model requests; text is stored only after
verification. New drafts still require the source to remain within normal inbox
age limits.

## Failure and recovery

| Symptom | Behavior and next step |
| :--- | :--- |
| Paid API returns 402 | Failed requests try free if enabled; paid is probed after its five-minute cooldown. Check account credit and per-key limits. |
| Free capacity is exhausted | Failed messages stay pending. Free-only mode never escalates to paid. |
| Both tiers fail | The worker waits five minutes and retries; it does not mark failed classifications complete. |
| IMAP tagging fails | Only confirmed writes are recorded. Other messages remain unclassified and are fetched again. |
| Filing or retention partially fails | The sweep exits nonzero. Messages not changed remain eligible for the next sweep; inspect the failed service or cron logs. |
| Worker is active but idle | Read Status and logs: caught-up, fetching, classifying, and retrying are distinct states. |
| A rule fails | The decision queue offers Retry. The failure does not disappear as a successful action. |
| Unsubscribe fails | The candidate remains pending. A separately requested block can still take effect. |
| Draft save fails | The prepared body is retained and reused. A stable draft Message-ID checks for an earlier successful append. |
| Stale forms after restart | Reload the page for a new form token; do not disable CSRF checks. |

Recovery intervals are checked between batches, not by a separate real-time
billing monitor. A long model request or a running batch can delay a probe.
Free fallback is best-effort: provider account restrictions can affect both tiers.

Subscription tracking compares delivery time with the successful unsubscribe
request. Older backlog mail and messages classified as transactional do not
resurface the request as new marketing. Retried messages are counted once.
Historical unsubscribe records without a request timestamp cannot establish that
a message arrived afterward.

### SELinux and system services

On an SELinux-enforcing host, a system service launched from a home-directory
virtual environment can fail with `203/EXEC` and `Permission denied`, even when
the same command works in a shell. Check the journal and audit log first. If the
executable context is the cause, an administrator can label that specific
environment instead of disabling SELinux:

```bash
sudo semanage fcontext -a -t bin_t '/home/your-user/tahor/venv/bin(/.*)?'
sudo restorecon -Rv /home/your-user/tahor/venv/bin
```

Use your actual checkout path, then restart the affected service. Oracle Linux
provides `semanage` in `policycoreutils-python-utils`. An SELinux-confined reverse
proxy may separately need permission to connect to the loopback application port.

## Configuration reference

`run.py` reads the private environment file before importing a component. Pass
`--env /path/to/config.env` **before** the component name for another instance.
No shell `source` command is required.

| Variable | Use |
| :--- | :--- |
| `FASTMAIL_EMAIL`, `FASTMAIL_APP_PASSWORD` | Mailbox login; use an app password |
| `FASTMAIL_HOST` | IMAP hostname; default `imap.fastmail.com`, SSL port 993 |
| `FASTMAIL_SMTP_HOST` | SMTP hostname; default `smtp.fastmail.com`, SSL port 465 |
| `OPENROUTER_API_KEY` | Hosted classification and OpenRouter rule/reply models |
| `GEMINI_API_KEY` | Legacy standalone classifier only; direct writing routes are disabled |
| `DATA_DIR` | Private prompt, vendor mappings, and Sieve proposal directory |
| `PROMPT_PATH` | Optional explicit classification prompt path |
| `VENDOR_BUCKETS_PATH` | Optional explicit routing map path |
| `TAHOR_DB_PATH` | Shared SQLite decisions database |
| `TAHOR_SETTINGS_PATH` | Shared speed/model/reply-rule settings |
| `TAHOR_STATE_DIR` | Worker batches, processed-ID audit, and local worker log |
| `TAHOR_STATUS_PATH` | Optional worker status snapshot override |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Review-app OAuth client |
| `ALLOWED_EMAIL` | The verified Google account allowed into the app |
| `BASE_URL` | Exact browser origin, including `https://` for a public deployment |
| `TAHOR_BIND` | Gunicorn bind address; keep the default `127.0.0.1:8420` behind a proxy |
| `TAHOR_DATA_PUSH` | `1` to enable private configuration pushes; default is local commits only |
| `WORKER_BATCH_SIZE` | Messages per classification batch; default 50 |
| `WORKER_SLEEP_BETWEEN_BATCHES` | Optional delay override; defaults to 0 seconds after fully applied paid-only batches, 45 seconds otherwise |
| `TAHOR_CLASSIFY_FREE_ENABLED` | Default `1`: free classification is available when policy permits it. Set `0` to block free classification regardless of policy |
| `TAHOR_PAID_CONCURRENCY` | Concurrent paid classifications; default 8, configurable from 1 to 64; effective concurrency also depends on batch size |
| `TAHOR_PAID_REQUEST_INTERVAL_SECONDS` | Minimum time between paid Vertex request starts, including retries; default 3 seconds, range 1–60 |
| `TAHOR_FREE_REQUEST_INTERVAL_SECONDS` | Minimum time between free Ling classification starts, including retries; default 5 seconds, range 1–60 |

Speed and model selections live in `settings.json` and are changed through the
web app. `CLASSIFY_BACKEND` is for the standalone `classify.py` utility; it does
not override the continuous worker’s Free/Paid/Auto setting.

The standalone classifier also supports a local OpenAI-compatible endpoint.
Direct Google requests are disabled pending account-specific privacy verification.
The integrated continuous worker uses the two OpenRouter tiers.
The paid model is `google/gemini-3.8-flash`, pinned to
`google-vertex/global` with provider fallback disabled, zero data retention,
and data collection denied. Requests use low reasoning effort and a 2,048-token
output budget. The endpoint does not advertise temperature support; do not
assume it honors the requested temperature. Up to eight requests overlap, with
starts paced three seconds apart across worker threads, including retries. The
unpaced deployment encountered HTTP 429 responses; accuracy benchmarks do not
establish sustained rate limits. The previous 40-request benchmark applied to
Nemotron, not this provider/model combination.

The selection was checked on 24 real messages and ten separate synthetic policy
cases. Completed Gemini 3.8 responses made no incorrect trash decisions in those
samples. One synthetic request timed out and passed the unchanged case on retry.
These limited evaluations do not establish a general accuracy guarantee.

The free Ling classifier is available by informed choice. The original 24-message
evaluation included five incorrect trash decisions, including important messages.
Ling rule drafting selected a wrong folder in one of eight cases; all generated
rule changes require review. Free reply drafts may contain invented commitments
or details and must be reviewed. Settings discloses these observed weaknesses;
free-only routing does not imply equivalent model accuracy.
Model availability changes: check your provider’s catalog if a selected model
stops responding. Paid estimates are not spending limits. Configure a provider
budget separately if you need one.

All OpenRouter classification requests require zero data retention and deny
provider data collection. These routing filters preserve the selected model and
classification prompt; an unavailable compliant endpoint returns an error rather
than relaxing privacy requirements. This applies equally to paid and free
routes. See
[OpenRouter's endpoint privacy controls](https://openrouter.ai/docs/guides/features/zdr).

## Retention and filing

| Setting | Default |
| :--- | :--- |
| `RETENTION_TRANSIENT_DAYS` | 7 |
| `RETENTION_STANDARD_DAYS` | 1095 |
| `FILING_READ_MIN_AGE_DAYS` | 3 (legacy fallback) |
| `FILING_UNREAD_MIN_AGE_DAYS` | 7 (legacy fallback) |
| `FILING_ROOT` | `Filed` |

Use **Settings → Time in the inbox** to configure the read and unread filing
delays. Defaults are three and seven days respectively; zero allows immediate
filing. Saved settings override the legacy environment variables and take effect
on the next sweep without restarting. These delays run from internal delivery
date, not from when a message was read. IMAP date searches are conservative and
can add up to one day at the boundary.

Filing grace protects only against moving between folders. It never postpones
classification, trash deletion, or ordinary retention. Both read and unread mail
are eligible for deletion when their retention period expires. Forever,
pending-review, and needs-attention tags exclude messages from retention cleanup.

The worker tags explicit trash with `delete-pending`, then deletes that exact UID.
The marker survives failed deletion and is retried on the next visit to the folder.
It no longer keeps a new sample from each trash sender. Existing messages already
marked forever remain protected. Older trash classifications without the new marker
continue through normal retention; a transient tag alone does not imply explicit trash.

The reference skip list is `Trash`, `Spam`,
`Sent`, `Drafts`, and `Archive`; it does not empty Trash. Mailbox names and special
folders vary across providers, so review the list in `retention_sweep.py` for yours.

Retention uses targeted UID expunge. Filing uses IMAP MOVE. Neither falls back to
expunging every message carrying the Deleted flag. Dry runs select mailboxes
read-only and do not close them with an expunging IMAP CLOSE command.

Routing maps accept complete sender domains. Older short-label mappings are also
read for compatibility. For example:

```json
{
  "billing.example.com": ["Finance/Statements", "Example Bank"],
  "shop.example": ["Shopping", "Example Store"]
}
```

New routing choices affect future filing. Existing folders are not silently
renamed or merged. Explicit trash is deleted without retaining a new sender sample.
Older forever-tagged samples remain protected.

### Read state after filing

Filing requires a completed classification in a supported category (receipt,
statement, or government tax), the appropriate read/unread age, and no
needs-attention, pending-review, delete-pending, or starred flag. These exclusions
also prevent filing from hiding messages that need attention.

After successful moves to a destination, the sweep marks eligible unread messages
there read using UID STORE. It never marks a source message read before MOVE, so
a rejected move leaves the message unread in INBOX. If setting the read flag
fails, the message remains eligible for retry and the sweep reports failure.

Every filing sweep also reconciles existing folders, even when there is nothing
to move from INBOX. It uses the same unread age, classification, and attention
criteria. It preserves classifications and folders and changes only the Seen flag.
INBOX, Drafts, Sent, Trash, Spam/Junk, Scheduled/Snoozed, and special-use aggregate
mailboxes are excluded from this backfill. Other existing folders, including
archives and imported folders, are eligible. Forever retention protects against
deletion but does not prevent an otherwise eligible filed message being marked read.

`run.py filing --dry-run` previews both moves and read-state changes without writing
flags. The normal daily filing job performs both operations for new and existing
installations. Repeated runs leave already-read messages alone.

## Private data and backups

Treat credentials, prompt customizations, sender rules, drafts, classification
batches, and logs as private. Hosted model calls transmit relevant content to the
selected provider. The web app escapes message text and protects mutations with
form tokens, but it remains a single-user tool; it is not a multi-tenant service.

The installer keeps personal state outside the code checkout. If you prefer a
private Git repository for the three configuration files:

```bash
git init "$HOME/.config/tahor/data"
git -C "$HOME/.config/tahor/data" add prompt.txt vendor_buckets.json sieve.txt
git -C "$HOME/.config/tahor/data" commit -m 'configure mailbox rules'
```

Configure a **private** remote yourself. Never add `config.env`, runtime state, or
credentials. Tahor stages only its named configuration files, not the whole
working directory. Git commits use your normal local identity. Private pushes
are disabled unless `TAHOR_DATA_PUSH=1`.

Use the included snapshot tool for a consistent SQLite backup plus your private
settings, prompt, vendor mappings, and Sieve proposal:

```bash
venv/bin/python scripts/private_backup.py \
  --env "$HOME/.config/tahor/config.env" \
  --destination "$HOME/tahor-backups"
```

The tool requires private storage outside Git, verifies file checksums and SQLite
integrity, and excludes credentials. Backups remain sensitive: the database can
contain drafts and mailbox metadata. Keep encrypted off-host copies and test a
restore before relying on them. See [private backups and restore](private-backups.md)
for the allowlist, verification command, service shutdown, and restore procedure.
Preserve the notification delivery ledger separately when moving an instance;
it is not included in these preference/database snapshots.

## Optional operational email

[Health alerts and daily summaries](notifications.md) place fixed health descriptions
and aggregate counts directly in your inbox, addressed from your account to itself.
They are opt-in, use IMAP APPEND over TLS, and make no model calls. Alerts require persistent problems;
healthy idle workers and progressing free-tier fallback do not trigger them.
The daily digest's timezone and hour are configurable. Reply drafts still require
you to review and send them in your mail client.

## Sieve proposals

Sender rules are enforced during classification. For provider-side filtering,
Tahor maintains a marked section inside `sieve.txt`, preserving custom content
outside that section. The app shows a proposal banner. Installing it in your
provider is a separate manual step; dismiss the banner only after applying it.
Alternatively, Fastmail users can explicitly enroll the
[isolated connector](fastmail-connector.md) to manage Tahor-owned whole-domain
rules. It has its own service account and credential storage; setup and fresh
authentication must be verified for each enrolled account.

Only whole-domain blocks generate Sieve discard rules. Marketing-only blocks
stay in the classifier: a receipt can carry `List-Unsubscribe`, so that header
alone is not a safe reason to discard it. The proposal lists marketing-only
domains as comments for reference.

Regenerate a proposal explicitly:

```bash
venv/bin/python run.py sieve
```

This writes a recommendation, not a live server filter. Never paste a generated
script over an unrelated provider script without reviewing the merged contents.

## What this release does not promise

Classification can be wrong. Review your prompt, ambiguous mail, and sweep
previews. Free models have limited capacity. App authentication requires your
Google OAuth configuration. Reply rules need your matching and writing
instructions. Provider rule installation requires either manual Sieve setup or
explicit Fastmail connector enrollment. Domain/TLS setup and billing controls
remain external steps. Optional health alerts and digests depend on the server
and IMAP connection; external monitoring is needed to detect complete host or
mail-delivery outages.

Tahor is designed around a single mailbox owner and tested primarily against
Fastmail. It does not support arbitrary IMAP servers without checking their
capabilities and folder conventions first. Tests cover failure paths and expected
behavior; they are not a guarantee that a mailbox or provider can never fail.

### Reviewing generated rules

Grok 4.6 is the recommended independent rule-writing choice, using the xAI ZDR
endpoint, low reasoning effort, and a 4,096-token budget for complete file edits.
Writing remains opt-in; selecting a model never authorizes its proposed changes.

AI rule generation creates a pending proposal rather than immediately changing
mail policy. The decision queue shows escaped file diffs or the exact sender action.
Approval uses the saved proposal without generating another answer. A hash binds
approval to its instruction, proposed result, and original files. Changes made
since preview require a fresh proposal; retries of an approved partial write accept
only the original or exactly approved file content. Repeated approval does not
reapply a completed decision. Rejection leaves unapplied rules unchanged.

Generated sender blocks must match a domain written explicitly in the instruction.
Brand inference, parent-domain broadening, and treating a single email address as
a whole-domain authorization are rejected. Manually selected sender controls and
vendor mappings retain their existing behavior.

### Optional private free-model guidance

To tune the experimental free classifier independently, put UTF-8 guidance in `DATA_DIR/free_classifier_guidance.txt`, or set `TAHOR_FREE_CLASSIFIER_GUIDANCE_PATH` to a private file. Tahor appends this text only for its Ling free classifier, before injecting your natural-language rules; paid classification stays unchanged. Missing default files are optional. Unreadable, non-regular, invalid UTF-8 or oversized files (over 64 KiB) leave messages pending instead of making requests; an explicitly configured missing path is also an error. Private backups include this file. Keep personal policy and examples outside the public checkout, and test changes against both preservation and deletion cases. Prompt tuning does not eliminate free-model mistakes.

### Large mailbox searches

Classification, filing, retention, and backlog counts search bounded UID ranges,
so a large folder does not exceed the IMAP client’s response-line limit. There is
no folder-size cutoff. Searches keep the UID boundary observed when selecting
the folder; newer arrivals are picked up on the next visit. A failed range
invalidates that search instead of applying a partial filing or deletion result.
