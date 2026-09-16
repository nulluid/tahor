# Run Tahor without administrator privileges

For an internet-facing installation, use a dedicated, non-login service account
with no sudo permissions. Keep the application and virtual environment owned by
the administrator. The service can modify mailbox data and runtime state, but
must not be able to replace the code it executes.

| Path | Owner and access | Purpose |
| --- | --- | --- |
| `/opt/tahor` | `root:root`; service can read, not write | Application and virtual environment |
| `/etc/tahor` | `root:tahor`, mode 0750 | Configuration directory |
| `/etc/tahor/config.env` | `root:tahor`, mode 0640 | Credentials and explicit paths |
| `/var/lib/tahor` | `tahor:tahor`, mode 0700 | Database, settings, private configuration repository and runtime state |

Create `tahor` with your distribution's account tools, home `/var/lib/tahor` and
a `nologin` shell. Do not grant it sudo or administrative group membership.
Install reviewed source and dependencies under `/opt/tahor` as an administrator.
Do not run the service from an administrator's home directory.

Set writable paths explicitly in `config.env`: `DATA_DIR`, `PROMPT_PATH`,
`VENDOR_BUCKETS_PATH`, `TAHOR_DB_PATH`, `TAHOR_SETTINGS_PATH`, `TAHOR_STATE_DIR`
and `TAHOR_SECRET_KEY_PATH`. Keep them beneath `/var/lib/tahor`.
Keep `TAHOR_DATA_PUSH=0` unless you separately configure narrowly scoped access
to a private repository. Local data commits need a Git identity but no GitHub
token or SSH key.

Generate and validate units from the installed checkout:

```bash
/opt/tahor/venv/bin/python /opt/tahor/scripts/system_services.py \
  --output /tmp/tahor-units
systemd-analyze verify /tmp/tahor-units/*.service /tmp/tahor-units/*.timer
```

The generator does not create accounts, move data or enable services. Review its
output before installing it in `/etc/systemd/system`. Inspect and replace old
drop-ins too: an old `ExecStart` or `User` override can undo the new configuration.
The default web unit is `tahor-web`; use `--web-name tahor-decision-app` when
replacing an existing unit with that name.

Services use `NoNewPrivileges`, empty capability sets, a read-only system
filesystem, inaccessible home directories, private temporary files, restricted
address families and disabled core dumps. Writable persistent storage is limited
to `/var/lib/tahor`. Filing and retention timers run at 09:00 and 09:15 UTC;
health checks run every fifteen minutes. Remove old Tahor cron entries before
enabling timers. Keep unrelated TLS renewal jobs.

Before migration, stop writers and back up SQLite consistently, settings, private
data, the cookie-signing key, units, drop-ins and schedules. Preserve the cookie
key to retain active sessions. Run `run.py --env /etc/tahor/config.env doctor` as
the service account, validate units with the host's systemd parser, and verify
actual process identities and filesystem access after startup. On SELinux hosts,
apply appropriate persistent file contexts; do not disable enforcement.

This isolates an ordinary service-process compromise from administrator access.
It cannot protect against root/kernel compromise or hide secrets from a process
authorized to use them. A connector holding full provider login credentials
needs a separate security boundary. Fastmail password/TOTP storage and automatic
provider-rule installation are not implemented by this setup.
