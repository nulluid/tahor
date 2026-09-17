# Back up private rules and mailbox state

Reply instructions, signatures, sender exclusions, and model choices belong to
an instance. Keep them private and make a recoverable copy outside the server.
The public repository contains the backup tool and synthetic tests, never an
owner’s settings or mailbox data.

## Make a verified snapshot

For the dedicated service-account layout:

```bash
sudo -u tahor /opt/tahor/venv/bin/python /opt/tahor/scripts/private_backup.py \
  --env /etc/tahor/config.env \
  --destination /var/lib/tahor/backups
```

For a user installation, run the script with your private `config.env` path and
an explicit private destination outside any Git checkout. The destination must
be owned by the executing user with mode `0700`; the tool creates it if absent.
Do not run as root when the restored files should belong to the service account.

Each timestamped snapshot includes:

- `settings.json`: reply rules, instructions, signatures, sender exclusions,
  model selections, inbox timing, and other preferences.
- `decisions.db`: decisions, sender rules, draft recovery state, and rule-match
  history, copied through SQLite’s consistent backup API, including committed WAL data.
- The configured classification prompt, vendor mapping, and Sieve proposal when
  present, stored under stable allowlisted filenames.
- A versioned manifest with file lengths and SHA-256 checksums.

Files use mode `0600`, snapshot directories `0700`. A snapshot appears under its
final name only after every file and the database have passed validation. The
manifest detects accidental corruption; it is not an authenticated signature.

SQLite is consistent even while running. Other files are read individually, so
pause workers and configuration edits if you need one coordinated point in time
across all settings and state. Backups do not include mailbox contents: those
remain with your email provider. Logs, transient batches, and provider sessions
are deliberately excluded.

## Automate an off-host recovery copy

For the hardened Linux deployment, run the receiver on a **separate trusted
computer** with Python and OpenSSH. The server never needs a key to that computer.
Use a dedicated SSH identity where possible; the remote account must be allowed
to run the installed recovery exporter through `sudo -n`. Verify the server's SSH
host key independently before the first run. Unknown or changed host keys fail
closed.

```bash
python3 scripts/offhost_backup.py \
  --host admin@mail.example.com \
  --identity ~/.ssh/tahor_backup \
  --destination ~/.local/share/tahor-recovery \
  --keep 28
```

The receiver asks `/opt/tahor/scripts/recovery_export.py` for a new snapshot,
downloads it over SSH, verifies all checksums and SQLite integrity, and atomically
publishes the completed copy. Only then does it acknowledge success to the server.
It retains the newest 28 verified copies; failed transfers or acknowledgements do
not prune existing copies. Snapshot creation and SSH transfers have time limits;
overlapping export or receiver runs fail without starting a second copy. Logs contain fixed status messages, not mailbox data or secrets.

Schedule this command every six hours with your computer's scheduler—for example,
a user systemd timer on Linux or a LaunchAgent on macOS. Use absolute paths to the
Python interpreter, script, identity and destination. A macOS calendar schedule
runs a missed job when the computer wakes; a powered-off or disconnected receiver
cannot back up the server. Keep its logs private and test the scheduled invocation,
not just the interactive command.

To receive an alert if no receiver has acknowledged a verified copy for 36 hours,
add this to the server's private configuration and enable health notifications:

```dotenv
TAHOR_OFFHOST_BACKUP_MAX_AGE_HOURS=36
```

A recovery directory contains `app/` with the normal private snapshot and, when
the optional connector is installed, `provider-ownership.json`. That file preserves
the connector's installation identity, account binding, owned rule IDs and any
uncertain creation intent. It contains **no password, TOTP seed, cookie or token**.
The outer `bundle.json` verifies both parts. You can verify a completed copy locally:

```bash
python3 -c 'import sys; sys.path.insert(0, "scripts"); from recovery_bundle import validate_completed; validate_completed(sys.argv[1]); print("Recovery copy verified")' \
  ~/.local/share/tahor-recovery/recovery-TIMESTAMP
```

Storage directories are mode `0700` and files `0600`, outside Git checkouts. The
copies are **not encrypted by this tool**. Use an encrypted disk or backup store
and protect the receiving computer as carefully as the private data it holds.
Checksums detect corruption; they are not a signature against a malicious party
that can replace the entire backup. The server retains three downloadable export
archives. Its ordinary local snapshots have a separate retention policy.

## Restore

First stop **all** Tahor services and timers, including the web app, worker,
draft watcher, filing, retention, and health checks. A running writer can undo
or corrupt a restore. The tool requires explicit acknowledgment and does not
stop services for you.

On a replacement machine, install Tahor and configure credentials first. Create
its normal settings and decisions database, and the required private data
directories with the correct service ownership. The restore command requires
this initialized destination so it can make a safety snapshot before changing it.
Then run:

```bash
sudo -u tahor /opt/tahor/venv/bin/python /opt/tahor/scripts/private_backup.py \
  --env /etc/tahor/config.env \
  --destination /var/lib/tahor/backups \
  --restore /var/lib/tahor/backups/backup-TIMESTAMP \
  --services-stopped
```

The tool validates the entire snapshot before touching live files. It rejects
unknown filenames, unexpected contents, symlinks, insecure snapshot permissions,
checksum mismatches, and invalid SQLite databases. It creates a new safety
snapshot of current state, then atomically replaces each restored file. The
whole multi-file restore is not a transaction: if it fails partway through,
keep services stopped, correct the failure, and retry the validated snapshot or
restore the reported safety snapshot. Files absent from the snapshot remain
unchanged.

Run the connection and configuration checks, inspect your rules in Settings,
and restart services and timers only after verification. Restoring old state
cannot undo messages already moved or deleted at the provider. Draft recovery
uses stable message identifiers, but check existing mailbox drafts after
recovering an old snapshot.

## Credentials need a separate recovery plan

The tool does not copy or overwrite `config.env`, IMAP app passwords, API keys,
OAuth secrets, browser sessions, Fastmail passwords, or TOTP seeds. Keep necessary account recovery information in a trusted
password manager. Re-enroll the [Fastmail connector](fastmail-connector.md) using
its documented procedure after server loss. If you use the off-host recovery
bundle, restore the app from its `app/` subdirectory, then recover provider ownership:

1. Keep automatic provider synchronization disabled. Enroll and verify the same
   Fastmail account on the replacement host.
2. Place a validated copy of the recovery directory in root-owned private storage
   outside any Git checkout; directories must be `0700`, files `0600`. Ownership
   checks deliberately reject an unreviewed upload owned by another user.
3. Stop `tahor-provider.service` and `tahor-provider-check.service`, then run:

```bash
sudo /opt/tahor/venv/bin/python /opt/tahor/scripts/restore_provider_ownership.py \
  /root/tahor-recovery/recovery-TIMESTAMP --connector-stopped
```

The command requires the newly enrolled account identity to match the backup and
refuses to overwrite conflicting ownership records. It restores only installation
identity and rule ownership, preserving the new credentials and session. Services
must remain stopped if interrupted; retry the same validated restore. Review
existing provider rules before restarting and enabling synchronization. Rules
created or edited after the snapshot may need manual reconciliation; uncertain
ownership stops instead of silently creating duplicates. An app-only snapshot
cannot reconstruct this journal.
