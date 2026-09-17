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

## Keep an off-host copy

A backup on the same disk does not cover server loss. Copy the complete snapshot
directory to a separate trusted machine or encrypted backup store, preserving
private permissions. Use SSH/SFTP or your established encrypted backup system;
do not upload snapshots to a public Git repository, issue, or artifact store.

The snapshot itself is **not encrypted**. For off-site storage, use a maintained
tool such as age or your backup provider’s encryption, keeping the decryption
key independently of the Tahor server. Check that you can decrypt and verify a
copy on another machine. Host-bound credential encryption alone does not provide
portable disaster recovery.

Verify the copied or decrypted directory without loading any account credentials:

```bash
python3 scripts/private_backup.py --verify /private/backups/backup-TIMESTAMP
```

A recurring scheduler can invoke the backup command, but it still needs a
separate off-host copy and retention policy. Check scheduler failures and verify
periodic copies; a configured timer alone is not evidence of a usable backup.

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
OAuth secrets, browser sessions, Fastmail passwords, TOTP seeds, or connector
ownership journals. Keep necessary account recovery information in a trusted
password manager. Re-enroll the [Fastmail connector](fastmail-connector.md) using
its documented procedure after server loss. Existing provider rules may remain
installed: inspect them before enabling a new connector, since this backup does
not reconstruct its private rule-ownership journal.
