# Operating Tahor

[← Tahor](../README.md) · [Setup](setup.md)

## Know what is running

The continuous worker classifies and tags INBOX. It does not run retention or
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

## Failure and recovery

| Symptom | Behavior and next step |
| :--- | :--- |
| Paid API returns 402 | Failed requests try free; paid is probed after its five-minute cooldown. Check account credit and per-key limits. |
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
| `GEMINI_API_KEY` | Only needed when choosing a Gemini rule/reply model |
| `DATA_DIR` | Private prompt, vendor mappings, and Sieve proposal directory |
| `PROMPT_PATH` | Optional explicit classification prompt path |
| `VENDOR_BUCKETS_PATH` | Optional explicit routing map path |
| `TAHOR_DB_PATH` | Shared SQLite decisions database |
| `TAHOR_SETTINGS_PATH` | Shared speed/model/reply-trigger settings |
| `TAHOR_STATE_DIR` | Worker batches, processed-ID audit, and local worker log |
| `TAHOR_STATUS_PATH` | Optional worker status snapshot override |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Review-app OAuth client |
| `ALLOWED_EMAIL` | The verified Google account allowed into the app |
| `BASE_URL` | Exact browser origin, including `https://` for a public deployment |
| `TAHOR_BIND` | Gunicorn bind address; keep the default `127.0.0.1:8420` behind a proxy |
| `TAHOR_DATA_PUSH` | `1` to enable private configuration pushes; default is local commits only |
| `TAHOR_NOTIFY_DRAFTS` | `1` to send yourself a summary after new drafts; disabled by default |
| `WORKER_BATCH_SIZE` | Messages per classification batch; default 50 |
| `WORKER_SLEEP_BETWEEN_BATCHES` | Delay between successful batches; default 45 seconds |

Speed and model selections live in `settings.json` and are changed through the
web app. `CLASSIFY_BACKEND` is for the standalone `classify.py` utility; it does
not override the continuous worker’s Free/Paid/Auto setting.

The standalone classifier also supports a local OpenAI-compatible endpoint and
Gemini. The integrated continuous worker currently uses the two OpenRouter tiers.
Model availability changes: check your provider’s catalog if a selected model
stops responding. Paid estimates are not spending limits. Configure a provider
budget separately if you need one.

## Retention and filing

| Setting | Default |
| :--- | :--- |
| `RETENTION_TRANSIENT_DAYS` | 7 |
| `RETENTION_STANDARD_DAYS` | 1095 |
| `FILING_READ_MIN_AGE_DAYS` | 7 |
| `FILING_UNREAD_MIN_AGE_DAYS` | 30 |
| `FILING_ROOT` | `Filed` |

Forever, pending-review, and needs-attention tags exclude messages from retention.
Retention selects only read mail. The reference skip list is `Trash`, `Spam`,
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
renamed or merged. When every message from a sender in a classification batch is
trash, one sample is retained forever and recorded only after successful tagging.
Later batches reuse that record instead of preserving another sample. Explicit
sender blocks bypass the holdback. Older forever-tagged samples remain protected.

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

For a consistent database backup, use SQLite’s backup API rather than copying a
file during a write. For the default installation:

```bash
mkdir -p "$HOME/tahor-backups"
python3 - <<'PY'
import sqlite3
from pathlib import Path
root = Path.home()
source = sqlite3.connect(root / '.config/tahor/state/decisions.db')
target = sqlite3.connect(root / 'tahor-backups/decisions.db')
source.backup(target)
target.close()
source.close()
PY
```

Back up the private data directory and settings too. Encrypt backups that leave
your machine. Restore paths consistently across every service and check file
permissions before restarting.

## Sieve proposals

Sender rules are enforced during classification. For provider-side filtering,
Tahor maintains a marked section inside `sieve.txt`, preserving custom content
outside that section. The app shows a proposal banner. Installing it in your
provider is a separate manual step; dismiss the banner only after applying it.

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
Google OAuth configuration. A first real reply trigger is your choice. Provider
Sieve installation, domain/TLS setup, and billing controls remain external steps.

Tahor is designed around a single mailbox owner and tested primarily against
Fastmail. It does not support arbitrary IMAP servers without checking their
capabilities and folder conventions first. Tests cover failure paths and expected
behavior; they are not a guarantee that a mailbox or provider can never fail.
