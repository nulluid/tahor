# Optional Fastmail connector

Tahor can manage whole-domain blocking rules through a separate credential service.
This is an **experimental integration with Fastmail's unpublished web API**. It is
opt-in; ordinary IMAP processing works without it. Manual Sieve installation remains
available and needs no full account-login credentials on your server.

The connector supports cookie-based session refresh and a fresh username/password/TOTP
login. Before changing provider rules, it also performs Fastmail's separate settings
reauthentication, supplying the password and TOTP when requested. It caches only the
provider's explicit authorization expiry and rereads rule state after authentication.
This flow has its own persistent 15-minute attempt cooldown; rejected credentials stop
further attempts until administrator enrollment. It does not assume a permission error
means an earlier rule write is safe to replay. A fresh login does not clear a rejected
settings-authentication attempt. Session refresh is a convenience, not a guarantee of
permanent access. A rejected
password, unsupported authentication challenge, or changed protocol stops automatic
sign-in and asks for administrator attention. Network errors retry; a persistent one-hour
login cooldown survives service restarts. It never sends SMS, resets credentials, or
changes account recovery settings. Normal classification continues independently.

## Security boundary

The web app runs as `tahor`; the connector runs as `tahor-provider`. Neither has sudo.
Code and virtualenv are root-owned. The connector exposes no listening socket. A
restricted directory carries only desired domain blocks and sanitized status. It
accepts no arbitrary Sieve, API methods, URLs, forwarding destinations, or login requests.
The web app cannot read the connector's password, TOTP seed, cookies, or bearer token.

The password and a **separate Tahor authenticator seed** are encrypted on disk using
`systemd-creds`, then delivered through systemd's read-only credential directory.
Session cookies and bearer tokens are stored in a mode-0700 connector directory with
mode-0600 files. They are credentials too: keep that directory out of ordinary backups,
repositories, logs, and support bundles. Core dumps are disabled. Requests use verified
TLS, exact Fastmail endpoint allowlists, no redirects or environment proxies, bounded
responses, and fixed connection/read timeouts.

This holds both login factors on one host. It grants the connector full account-login
authority even though its request interface exposes only domain blocking. Root, a kernel
compromise, or compromise of the connector itself can defeat this boundary. Host-key
encryption does not protect a stolen credential together with its decryption key. This
is not scoped Sieve OAuth or a replacement for independent security review. Fastmail
can change or withdraw these interfaces.

## Install and enroll

Requires Linux with systemd 252 or newer, `systemd-creds`, administrator access, and the
[hardened system deployment](service-isolation.md) at `/opt/tahor` with the `tahor` user.
The installer creates a non-login `tahor-provider` account, bridge directories, hardened
units, and an encrypted empty credential placeholder. It leaves synchronization off.

```sh
sudo /opt/tahor/venv/bin/python /opt/tahor/scripts/install_provider.py
sudo systemctl restart tahor-decision-app
sudo /opt/tahor/venv/bin/python /opt/tahor/scripts/enroll_fastmail.py
```

If your web service is named `tahor-web`, restart that service instead.
Run enrollment in an interactive terminal on the host, reached through SSH when remote.
Never supply passwords or seeds in command arguments, chat, issue reports, or Settings.

In Fastmail, open **Settings → Privacy & Security → Manage two-step verification →
Add verification device → Authenticator app**. Keep your existing authenticator enabled.
The enrollment command checks your username, then validates the hidden manual setup
key before asking for the hidden account password. It accepts standard 128-bit
keys (including 26-character Base32 keys), spaces and grouping hyphens. Invalid
fields receive a specific explanation and up to three attempts; nothing is saved
until all fields validate and you confirm the device.
It displays a current six-digit code so you can confirm the new device in Fastmail and
name it **Tahor**. Finish that provider step before typing `saved` in the terminal.
The script encrypts the credential and tests a fresh server-side sign-in, including
TOTP and settings reauthentication, under the restricted service identity. It never tests with a mailbox write.

After successful verification, use **Settings → Automatic provider rules → Enable**.
Enrollment is not proof that filtering is installed: wait for **Provider rules verified**.
The connector checks every five minutes. Only `block_all` sender rules are synchronized;
marketing-only blocks remain in Tahor so receipts can be preserved. Up to 250 whole-domain
rules are supported by this experimental connector.

## Rule ownership and recovery

The connector creates individually named Fastmail Rule objects and journals their IDs.
It never replaces your shared custom Sieve sections. Before removal, it checks that the
owned rule still matches the generated rule. Manual changes or deletions cause a conflict,
not an automatic overwrite or recreation. A lost creation response is reconciled against
its durable intent; ambiguous outcomes stop instead of creating duplicates.

This is **not an atomic compare-and-swap guarantee**. Fastmail's SieveBlocks endpoint
accepted deliberately stale `ifInState` values in testing. Individual Rule ownership
avoids replacing unrelated rules, but a simultaneous edit to a Tahor-owned rule can still
race with removal. Edit ordinary rules normally; turn synchronization off and wait for
any in-flight request to finish before editing a Tahor-owned rule. Existing earlier
custom Sieve rules that stop processing can prevent later ordinary rules from running.

Turning synchronization off stops future work, but an already submitted provider request
may finish. Installed rules remain active. To remove one, unblock its domain in Tahor while
synchronization is enabled and wait for verification. To remove all, unblock all managed
domains first, or disable synchronization and remove the named Tahor rules in Fastmail.
Do not delete the connector's ownership journal while its rules are installed.

If credentials are rejected, correct or re-enroll them with the terminal command above;
that explicit administrator operation allows a new test. A protocol or rule conflict
requires investigation rather than repeated destructive retries. Status contains no
provider response body or authentication secret:

```sh
systemctl status tahor-provider
journalctl -u tahor-provider --since today
```

For revocation, disable synchronization, stop/disable `tahor-provider`, revoke its session
and named verification device in Fastmail, and remove its encrypted credential and private
session state. Rotate the account password if the host may have been compromised. Removing
local files alone does not revoke a session or remove installed provider rules.

## Verification scope

Tests cover RFC 6238 vectors, full-login sequencing, rejected credentials, restart-safe
cooldowns, settings password/TOTP challenges and expiry, rejection persistence across
fresh logins, session expiry, endpoint restrictions, private-file permissions, rule ownership,
uncertain writes, request validation, sanitized status, and authenticated/CSRF-protected
Settings actions. A live disabled-rule create/read/remove test preserved all existing
rules. Each account still needs a successful fresh-login enrollment test; mocked tests do
not establish that an account has been enrolled or that an unpublished API will stay stable.

Fastmail documents [multiple verification devices and TOTP enrollment](https://www.fastmail.help/hc/en-us/articles/360058752374-Using-two-step-verification-2FA).
Its [Sieve FAQ](https://www.fastmail.help/hc/en-us/articles/360058753814-Sieve-frequently-asked-questions)
describes the supported manual editing path.
