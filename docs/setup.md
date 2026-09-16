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
./install.sh
```

The interactive installer asks for the mailbox email, IMAP app password, and
OpenRouter key. Passwords are hidden while entered. Nothing starts automatically.

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
| `--mode free` | Free classification; default for new installations |
| `--mode paid` / `--mode auto` | Explicitly enable paid classification capacity |
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

The services restart after failures. The draft watcher stays inactive until you
add a reply trigger in Settings. A trigger should be a sender whose messages you
actually want drafted, not an entire high-volume domain chosen just for testing.

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
Unmapped vendors go under `Filed/_Unsorted` and get a routing decision in the app.
Saving a routing rule affects future filing; it does not silently relocate older
messages already filed elsewhere.

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
systemctl --user enable --now tahor-healthcheck.timer
systemctl --user list-timers 'tahor-*'
```

Filing runs at 09:00 and retention at 09:15 in the server’s timezone. The health
check runs every fifteen minutes and exits nonzero when worker status is missing,
stale, or retrying after failure. Inspect failures through systemd or connect them
to your own monitoring. It does not send notification email by default.

## 6. Review optional features

- **Reply drafts:** add a sender or domain in Settings, then choose a reply model.
  Drafts appear in both the app and the mailbox’s Drafts folder. Optional summary
  notifications require `TAHOR_NOTIFY_DRAFTS="1"` and SMTP credentials.
- **Free-text rules:** submit an instruction from the decision queue. A model can
  update routing or classification rules; failures stay visible for retry.
  Instructions that require new code stay pending and are recorded for manual
  implementation; submitting one does not change application code.
- **Sender blocks:** apply in the worker immediately. For earlier provider-side
  blocking, review the generated Sieve proposal and install it in your provider’s
  Sieve editor. Removing a block also requires installing the updated proposal.
  Use **Refresh proposal** on the decisions page to retry a failed generation.
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
