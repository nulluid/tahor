# From checkout to a running mailbox assistant

[← Tahor](../README.md)

This guide covers a single-user installation on Linux. The worker and local
preview also run on macOS. Have an IMAP app password and an OpenRouter API key
ready. Optional Google OAuth setup is required for the real review app.

## 1. Install

On a Debian/Ubuntu server, install prerequisites if needed:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv git
```

Then:

```bash
git clone https://github.com/nulluid/tahor.git
cd tahor
./install.sh --mode paid_only
```

The interactive installer asks for the mailbox email, IMAP app password, and
OpenRouter key. Passwords are hidden while entered. Nothing starts automatically.
The example explicitly selects always-paid classification. Omitting `--mode paid_only`
uses always-free classification, which never incurs paid requests. Review the
free-model disclosures before processing: the free classifier can incorrectly
classify important messages as trash. Settings provides four separate
policies for classification, reply writing, and rule writing.

For a different mail provider:

```bash
./install.sh --imap-host imap.example.com --smtp-host smtp.example.com
```

To prepare an instance without entering secrets yet:

```bash
./install.sh --non-interactive --email you@example.com
```

Edit `~/.config/tahor/config.env` afterward. Empty credentials fail the setup check
rather than starting a partly configured worker. The environment file accepts
`KEY="value"` assignments; values are parsed as data, not executed as shell code.

Useful options:

| Option | Purpose |
| :--- | :--- |
| `--config-dir /path/to/instance` | Put credentials and runtime state in a different directory |
| `--data-dir /path/to/private-repo` | Keep prompts and mappings in a separate private checkout |
| `--mode free` | Always free; default for new installations, with known model limitations |
| `--mode paid_only` | Always paid; retry failures without free fallback |
| `--mode paid` | Paid with free fallback during failures |
| `--mode auto` | Free first; allow paid for failures or a projected backlog over four hours |
| `--base-url https://mail-tools.example.com` | Set the review app’s public URL |
| `--systemd-dir ~/.config/systemd/user` | Generate unattended user services and timers |

Existing private configuration and data files are preserved on repeated setup.
Change an established instance through its Settings page or configuration file.

## 2. Check before processing

```bash
venv/bin/python run.py doctor
venv/bin/python run.py doctor --check-imap --check-model
```

The first command checks local configuration. The second logs into IMAP read-only
and sends a synthetic message to the configured classification tier. It does not
apply flags, move mail, delete mail, or send an email. In paid mode the model test
can incur a small provider charge.

The check reports support for `MOVE` and `UIDPLUS`. The corresponding sweeps
refuse to use an unsafe global-expunge fallback if a capability is absent.
Fastmail is the reference installation; test other IMAP providers before relying
on unattended filing or retention.

Start classification in the foreground:

```bash
venv/bin/python run.py worker
```

Use Ctrl-C to stop. The worker records progress only after applying message tags,
so it can resume. It discovers all selectable folders, including archives, imported
mail, Sent, Drafts, Spam, and Trash, with no folder-size ceiling. Each folder gets
one bounded batch per pass, with INBOX checked between folders. Folder discovery
repeats every pass so newly created folders join automatically. Classification
adds tags immediately and deletes explicit trash; filing and age-based retention
remain separate scheduled operations.

Each batch combines recent arrivals with a rotating portion of older unclassified
mail. Failed messages remain eligible for retry without blocking older messages.
The private `fetch_cursors.json` state preserves that rotation across restarts.
The worker polls every ten minutes after a complete pass finds no unclassified mail.

## 3. Enable the review app

Create a **Web application** OAuth client in Google Cloud. Configure the consent
screen and allow your own account as a test user if the client is in testing mode.
Register this exact redirect URI:

```text
http://localhost:8420/auth/google/callback
```

For a public HTTPS endpoint, use its origin instead of localhost. Set these values
in your private `config.env`:

```dotenv
GOOGLE_CLIENT_ID="your-client-id"
GOOGLE_CLIENT_SECRET="your-client-secret"
BASE_URL="http://localhost:8420"
ALLOWED_EMAIL="you@example.com"
```

Only the configured, verified Google email can sign in. It can differ from the
IMAP address if needed. See [Google’s web-server OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server)
for provider-side setup.

```bash
venv/bin/python run.py doctor --web
venv/bin/python run.py web
```

Open `http://localhost:8420`. On a remote server, a tunnel avoids exposing the port:

```bash
ssh -L 8420:127.0.0.1:8420 user@your-server
```

For public access, keep Gunicorn bound to loopback and use a TLS reverse proxy.
Example Caddy configuration:

```caddyfile
mail-tools.example.com {
    reverse_proxy 127.0.0.1:8420
}
```

The optional daily digest links to the instance at `BASE_URL`; set an address
you can reach from the device where you read email.

Point DNS at the server, allow the proxy’s HTTPS traffic, and set `BASE_URL` and
the Google redirect URI to the same HTTPS origin. Use your proxy’s documented
installation and certificate workflow. Do not expose the Flask development server.

## 4. Run independently of your terminal

Generate systemd user units:

```bash
venv/bin/python setup_tahor.py --non-interactive \
  --systemd-dir "$HOME/.config/systemd/user"
systemctl --user daemon-reload
systemctl --user enable --now tahor-backlog-worker tahor-web tahor-draft-replies
```

To keep these running after logout, an administrator can enable lingering:

```bash
sudo loginctl enable-linger "$USER"
```

The services restart after failures. Drafting begins after you enable a reply
rule in Settings. Choose a focused description of messages you actually want
answered, or match a specific sender or domain. Existing sender triggers remain
compatible.

Check services and logs:

```bash
systemctl --user status tahor-backlog-worker tahor-web tahor-draft-replies
journalctl --user -u tahor-backlog-worker -n 50 --no-pager
venv/bin/python run.py status
```

Generated units reference this checkout and virtual environment by absolute path.
If you move either, regenerate the units and reload systemd.

## 5. Preview filing and retention

Classification is useful on its own. Review the tags and your prompt first.
Then preview the separate sweeps:

```bash
venv/bin/python run.py filing --dry-run
venv/bin/python run.py retention --dry-run
```

Filing moves eligible receipts, statements, and tax messages into vendor folders.
The default filing delays are three days for read mail and seven for unread mail.
Change either under **Settings → Time in the inbox**. Classification happens
immediately, regardless of those delays.
After a successful move, qualifying unread mail is marked read in its destination.
The filing sweep also checks existing folders, so first-time installations clean
up already-filed unread mail using the same rules. It leaves starred messages,
mail needing attention or review, and unclassified mail untouched.
Unmapped vendors go under `Filed/_Unsorted`. The rule writer examines actual sender addresses and message samples, automatically maps confident routine receipts and statements, and brings uncertain cases to the app. Shared delivery domains can have separate merchant mappings. Enable rule AI in Settings to use this automation.
Saving a routing rule affects future filing and a bounded reconciliation of
eligible receipts and statements in `Filed/_Unsorted`. Messages already filed in
other destinations are not silently relocated.

Retention permanently deletes expired messages, whether read or unread. Filing
delays do not postpone deletion. The worker deletes explicit trash immediately.
Retention protects forever,
pending-review, and needs-attention messages, and skips the folders listed in
[operations](operations.md#retention-and-filing). Its age is based on the mailbox’s
internal delivery date, not the date Tahor classified the message. Old mail, read or unread,
can therefore be eligible on the first sweep.

When the previews match your intent:

```bash
systemctl --user enable --now tahor-filing.timer tahor-retention.timer
systemctl --user enable --now tahor-healthcheck.timer tahor-decisions.timer
systemctl --user list-timers 'tahor-*'
```

Filing runs at 09:00 and retention at 09:15 in the server’s timezone. The health
check runs every fifteen minutes and exits nonzero when worker status is missing,
stale, or retrying after failure. Inspect failures through systemd or connect them
to your own monitoring. It does not send notification email by default. To opt into self-addressed
health alerts and a daily summary, enable the separate
[notification settings and timer](notifications.md).

## 6. Review optional features

- **Reply drafts:** open **Settings → Reply rules**. Give the rule a name, choose
  natural-language, sender, or domain matching, and describe the reply’s content
  and tone. Add a signature and choose one to three body sentences. Save the
  rule, then expand its matched senders to exclude anyone you do not want to
  answer. You can also set a filing folder for eligible low-attention matches.
  Drafts appear in your mailbox’s Drafts folder, linked to the original message;
  the original stays unread. Review, edit, and send in your email client.
  There is no web draft editor and no automatic sending.
  Initial matching is bounded to recent inbox mail; the normal three-day read
  and seven-day unread filing windows still govern drafting eligibility.
  Uncertain matches wait for review rather than generating a reply.
  Reply writing starts disabled. Choose models and a spending policy
  in Settings; reply writing is independent of classification policy.
- **Free-text rules:** first select an AI rule model in Settings, then submit an
  instruction from the decision queue. A model can
  propose routing or classification changes. Review the displayed diff or sender
  action and explicitly approve it before application; failures stay visible for retry.
  Domain blocks require an explicit full domain, not only a brand or email address.
  Instructions that require new code stay pending and are recorded for manual
  implementation; submitting one does not change application code.
- **Sender blocks:** apply in the worker immediately. For earlier provider-side
  blocking, review the generated Sieve proposal and install it in your provider’s
  Sieve editor. Removing a block also requires installing the updated proposal.
  Use **Refresh proposal** on the decisions page to retry a failed generation.
  Fastmail users can alternatively install the optional
  [isolated credential connector](fastmail-connector.md). It requires explicit
  enrollment and account verification; the ordinary IMAP app password does not
  authorize provider-settings management.
- **Private version history:** initialize a private Git repository at `DATA_DIR`.
  Tahor commits configuration edits locally. `TAHOR_DATA_PUSH="1"` explicitly
  enables pushing to that repository’s configured origin.

## Updating

```bash
git pull --ff-only
venv/bin/python -m pip install -r requirements.txt
venv/bin/python run.py doctor
systemctl --user restart tahor-backlog-worker tahor-web tahor-draft-replies
```

Back up private data and the database before updating. Keep the virtual environment
and code checkout separate from your personal configuration. See the
[operations guide](operations.md) for recovery and backup details.

### Email-based unsubscribe requests

Fastmail must allow the configured app password to send mail through SMTP.
Choose **Mail (IMAP/POP/SMTP)** access when creating that app password. Reading
mail and delivering Tahor digests use IMAP, so those working does not establish
that SMTP sending works. Optionally configure `FASTMAIL_SMTP_USERNAME` and
`FASTMAIL_SMTP_APP_PASSWORD` in the protected environment file to use a separate
sending credential. Do not use your account password or authenticator key here.
Sender website links may require you to confirm the unsubscribe in your browser.
