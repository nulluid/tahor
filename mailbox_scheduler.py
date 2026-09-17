"""Fair productive-mailbox scheduling with one bounded discovery connection."""
from collections import deque
import imaplib
import threading
import time

from mailbox_paths import list_mailboxes, quote_mailbox
from mailbox_search import search_uids

RETENTION = ('retention-forever', 'retention-standard', 'retention-transient', 'retention-pending-review')


def has_work(conn, mailbox):
    status, _ = conn.select(quote_mailbox(mailbox), readonly=True)
    if status != 'OK':
        raise RuntimeError('Mailbox discovery select failed')
    unclassified = '(' + ' '.join('UNKEYWORD ' + key for key in RETENTION) + ')'
    trash = '(KEYWORD delete-pending UNKEYWORD retention-forever UNKEYWORD retention-pending-review UNKEYWORD needs-attention OR SEEN UNKEYWORD reply-protected)'
    _, rows = search_uids(conn, 'OR', unclassified, trash)
    return bool(rows and rows[0])


class ProductiveMailboxes:
    def __init__(self, clock=time.monotonic, inbox_interval=30, retry_seconds=30):
        self.clock = clock
        self.inbox_interval = inbox_interval
        self.retry_seconds = retry_seconds
        self.condition = threading.Condition()
        self.mailboxes = {'INBOX'}
        self.active = set()
        self.queue = deque()
        self.queued = set()
        self.retry_at = {}
        self.inbox_at = 0
        self.inbox_streak = 0
        self.inbox_known_empty = False
        self.trash = {'Trash'}
        self.backend_retry_at = 0
        self.busy = None
        self.scanning = True
        self.discovery_failed = False

    def _enqueue(self, mailbox):
        if mailbox != 'INBOX' and mailbox not in self.queued:
            self.queue.append(mailbox)
            self.queued.add(mailbox)

    def discovered(self, mailboxes, trash=None):
        with self.condition:
            self.mailboxes = set(mailboxes)
            if trash is not None:
                self.trash = set(trash)
            self.active.intersection_update(self.mailboxes)
            self.retry_at = {key: value for key, value in self.retry_at.items() if key in self.mailboxes}
            self.condition.notify_all()

    def observe(self, mailbox, work):
        with self.condition:
            if mailbox not in self.mailboxes:
                return
            # An empty discovery result must not cancel work currently being
            # processed; completion will decide whether another batch is due.
            if work:
                self.active.add(mailbox)
                if mailbox == 'INBOX':
                    self.inbox_known_empty = False
                    self.inbox_at = 0
                self._enqueue(mailbox)
            elif mailbox != self.busy:
                self.active.discard(mailbox)
            self.condition.notify_all()

    def next_mailbox(self):
        with self.condition:
            now = self.clock()
            if now < self.backend_retry_at:
                return None
            inbox_ready = ('INBOX' in self.mailboxes and now >= self.inbox_at
                           and now >= self.retry_at.get('INBOX', 0))
            if inbox_ready and self.inbox_streak < 3:
                self.inbox_streak += 1
                self.busy = 'INBOX'
                return 'INBOX'
            for _ in range(len(self.queue)):
                mailbox = self.queue.popleft()
                self.queued.discard(mailbox)
                if mailbox not in self.mailboxes or mailbox not in self.active:
                    continue
                if mailbox in self.trash and 'INBOX' in self.mailboxes and not self.inbox_known_empty:
                    self._enqueue(mailbox)
                    continue
                if now < self.retry_at.get(mailbox, 0):
                    self._enqueue(mailbox)
                    continue
                self.inbox_streak = 0
                self.busy = mailbox
                return mailbox
            if inbox_ready:
                self.inbox_streak += 1
                self.busy = 'INBOX'
                return 'INBOX'
            return None

    def completed(self, mailbox, status, backend_retry=300):
        with self.condition:
            self.busy = None
            now = self.clock()
            if mailbox == 'INBOX':
                self.inbox_at = now + self.inbox_interval if status == 'empty' else now
            if status == 'empty':
                if mailbox == 'INBOX':
                    self.inbox_known_empty = True
                self.active.discard(mailbox)
                self.retry_at.pop(mailbox, None)
            else:
                self.active.add(mailbox)
                if mailbox == 'INBOX':
                    self.inbox_known_empty = False
                self._enqueue(mailbox)
                if status in ('error', 'backend_unavailable'):
                    self.retry_at[mailbox] = now + (backend_retry if status == 'backend_unavailable' else self.retry_seconds)
                    if status == 'backend_unavailable':
                        self.backend_retry_at = now + backend_retry
                else:
                    self.retry_at.pop(mailbox, None)
            self.condition.notify_all()

    def wait(self, maximum=30):
        with self.condition:
            now = self.clock()
            deadlines = [max(self.inbox_at, self.retry_at.get('INBOX', 0))] if 'INBOX' in self.mailboxes else []
            deadlines += [value for key, value in self.retry_at.items() if key in self.active]
            if self.backend_retry_at > now:
                deadlines = [self.backend_retry_at]
            positive = [value-now for value in deadlines if value > now]
            self.condition.wait(timeout=min([maximum] + positive))

    def snapshot(self):
        with self.condition:
            return {'mailbox_count': len(self.mailboxes), 'productive_mailbox_count': len(self.active),
                    'discovery_scanning': self.scanning, 'discovery_failed': self.discovery_failed,
                    'backend_retrying': self.clock() < self.backend_retry_at}


class Discovery(threading.Thread):
    """Read-only sweeps share one connection, independent of slow model batches."""
    def __init__(self, scheduler, connect, log, interval=300, folder_pause=.025):
        super().__init__(name='tahor-mailbox-discovery', daemon=True)
        self.scheduler = scheduler
        self.connect = connect
        self.log = log
        self.interval = interval
        self.folder_pause = folder_pause
        self.stop_event = threading.Event()
        self.resume_after = None

    def scan_once(self):
        conn = None
        try:
            conn = self.connect()
            listed = list_mailboxes(conn)
            names = [name for name, _ in listed]
            self.scheduler.discovered(names, {name for name, flags in listed if '\\trash' in flags or name.lower() == 'trash'})
            if self.resume_after in names:
                cut = names.index(self.resume_after) + 1
                names = names[cut:] + names[:cut]
            with self.scheduler.condition:
                self.scheduler.scanning = True
            for name in names:
                if self.stop_event.is_set():
                    return False
                self.resume_after = name
                try:
                    work = has_work(conn, name)
                except (imaplib.IMAP4.error, OSError):
                    # Transport/protocol errors may leave the stream unusable.
                    # Resume beyond this folder next sweep, avoiding starvation.
                    self.scheduler.observe(name, True)
                    raise
                except RuntimeError:
                    self.scheduler.observe(name, True)
                    self.log('Mailbox discovery could not verify a folder; queued for retry')
                    continue
                self.scheduler.observe(name, work)
                if self.stop_event.wait(self.folder_pause):
                    return False
            self.resume_after = None
            with self.scheduler.condition:
                self.scheduler.discovery_failed = False
            return True
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
            with self.scheduler.condition:
                self.scheduler.scanning = False
                self.scheduler.condition.notify_all()

    def run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.scan_once()
                delay = max(1, self.interval - (time.monotonic() - started))
            except Exception:
                with self.scheduler.condition:
                    self.scheduler.discovery_failed = True
                    self.scheduler.condition.notify_all()
                self.log('Mailbox discovery connection failed; retrying in 30 seconds')
                delay = 30
            self.stop_event.wait(delay)
