# Health alerts and daily summaries

Tahor can place operational notices **directly in your own inbox** using IMAP
APPEND over TLS. Each notice is addressed from your account to itself. This is a
mailbox write, not SMTP delivery or a message sent to another person. These messages contain fixed health descriptions and
aggregate counts, never email excerpts, draft bodies, provider error responses,
or credentials. No model call is needed. Reply drafts remain unsent until you
send them yourself in your mail client.

Both notification types are off by default. Notifications use the same IMAP
credentials as ordinary processing; no additional SMTP permission is needed.

## Enable notifications

Add these settings to your private `config.env`:

```dotenv
TAHOR_NOTIFY_HEALTH="1"
TAHOR_NOTIFY_DIGEST="1"
TAHOR_NOTIFY_TIMEZONE="America/Denver"
TAHOR_NOTIFY_HOUR="9"
```

Use an IANA timezone name. The default is `UTC`, with a digest after 09:00 local
time. Either notification type can be enabled independently. Both From and To
are `FASTMAIL_EMAIL`; there is no separate recipient setting. Notices arrive
unread, already tagged as notifications with standard retention, so they do not
need a model classification.

For a user-service installation, regenerate units if needed, then enable the
optional timer:

```bash
venv/bin/python setup_tahor.py --non-interactive \
  --systemd-dir "$HOME/.config/systemd/user"
systemctl --user daemon-reload
systemctl --user enable --now tahor-notifications.timer
```

For the dedicated-account deployment, generate the separate hardened units:

```bash
/opt/tahor/venv/bin/python /opt/tahor/scripts/notification_services.py \
  --user tahor --config /etc/tahor/config.env \
  --state /var/lib/tahor --python /opt/tahor/venv/bin/python \
  --output /tmp/tahor-notification-units
systemd-analyze verify /tmp/tahor-notification-units/*.service \
  /tmp/tahor-notification-units/*.timer
sudo install -m 0644 /tmp/tahor-notification-units/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tahor-notifications.timer
```

Review generated paths before installing. On SELinux hosts, restore the installed
unit files' labels before loading them. Enabling the timer alone does not enable
mailbox notices; the private opt-in settings must also be set.

## What triggers a message

The timer checks every 15 minutes. A health condition must appear in **three
consecutive, spaced checks** before an alert is sent. Checks made manually in
quick succession do not advance that counter.

Conditions include missing worker status, a heartbeat older than an hour, no
batch progress for over an hour while the worker is active, and an enabled
Fastmail connector requiring authentication or administrator review. An idle
worker that is caught up is healthy. Falling back from paid to free models does
not trigger an alert while processing continues. A continuing incident receives
one alert; a later recurrence after recovery can receive another.

The daily summary includes worker health, pending review decisions, draft
preparations completed during the preceding 24 hours, and preparations awaiting
retry. Draft counts come from Tahor's local journal, **not** the current contents
of Fastmail's Drafts folder. The first check after the configured hour sends that
day's summary; restarts do not append it again. There is no backfill of missed days.

## Delivery failures and private state

The delivery ledger is `notifications.json` beside `decisions.db`, with private
permissions and a process lock. `TAHOR_NOTIFICATION_STATE` can override that
path. Keep this ledger when moving or restoring an instance.

Failed connections and explicit IMAP rejection retry with backoff. A connection
loss during APPEND can mean the mailbox accepted the notice. Tahor records the
attempt before writing and searches for its stable Message-ID before retrying.
Search hits are confirmed using the exact Message-ID, UID, sender, recipient,
and notification header. After an ambiguous append, reconciliation searches all
selectable folders, so moving the notice does not normally create another copy.
An unavailable folder or failed search leaves the attempt pending confirmation.
Successful APPEND is also read back before being marked delivered.

This recovery cannot guarantee exactly-once appearance if a notice is permanently
deleted before an interrupted append is reconciled. Preserve the private ledger;
deleting it discards delivery history. Legacy uncertain SMTP attempts, if any,
remain held for administrator confirmation rather than being blindly retried.

Check the timer and service logs:

```bash
systemctl --user status tahor-notifications.timer
journalctl --user -u tahor-notifications.service -n 30 --no-pager
```

`venv/bin/python run.py notify` runs a check immediately and **may add an unread inbox notice**
when notifications are enabled and due. Notification delivery itself depends on
the host, network, and IMAP credentials. Use external monitoring if you need alerts
when the whole server or its email connection is unavailable.

## Off-host recovery monitoring

Set `TAHOR_OFFHOST_BACKUP_MAX_AGE_HOURS=36` after configuring the off-host receiver.
Tahor then treats a missing or overdue receiver acknowledgement as a health
problem. The timestamp is updated only after the receiver validates its copy,
not merely when the server creates an archive. Normal health-alert persistence
and deduplication apply. Check the receiving computer's scheduler, connectivity
and private status file if an alert arrives.
