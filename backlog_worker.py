#!/usr/bin/env python3
"""
Continuously classify unprocessed mail, running unattended.

Loop: discover every selectable mailbox, fetch a batch of not-yet-processed
messages -> classify -> turn into ops -> apply real IMAP keywords -> record
the batch's message-ids as processed.

Once a full pass over every mailbox yields nothing new, the backlog is
exhausted -- this naturally becomes a steady-state "check for new mail"
loop from that point on (fetch always dedupes against everything already
processed), so no separate mode switch is needed.

Everything runs in-process (no subprocess/fork of the other pipeline
scripts) -- classify.py's own thread pool making HTTPS calls hung
indefinitely when invoked via subprocess.run() on this box (reproduced
repeatedly, including from a bare interactive `subprocess.run(...)` with
no systemd/session involved -- looks like a fork+SSL/threading lock
interaction). Importing and calling the same functions directly sidesteps
the fork entirely.

Run under systemd (Restart=always) for durability across reboots/crashes.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import fetch_batch
import classify
import process_batch
import keyword_tool
import mailbox_settings
import runtime_status
import retention_sweep
from data_changes import atomic_write
from mailbox_paths import quote_mailbox, list_mailboxes

# PROMPT_PATH can point anywhere, including a separate private repo, if you
# want your prompt/config to have its own tracked history -- it's just an
# env var, not a hardcoded assumption. Defaults to this repo's own
# gitignored prompt.txt (see config.py's vendor_buckets() for the same
# convention).
PROMPT_PATH = Path(os.environ.get("PROMPT_PATH", Path(os.environ.get("DATA_DIR", REPO)) / "prompt.txt"))
STATE_DIR = Path(os.environ.get("TAHOR_STATE_DIR", REPO))
STATE_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_IDS_PATH = STATE_DIR / "processed_message_ids.txt"
LOG_PATH = STATE_DIR / "logs" / "backlog_worker.log"

RETENTION_KEYWORDS = {"retention-forever", "retention-standard", "retention-transient", "retention-pending-review"}
BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", 50))
# Free and paid backends have independent concurrency limits in classify.py.
SLEEP_BETWEEN_BATCHES = int(os.environ.get("WORKER_SLEEP_BETWEEN_BATCHES", 45))
PAID_BATCH_DELAY = int(os.environ.get("WORKER_SLEEP_BETWEEN_BATCHES", 0))
SLEEP_WHEN_IDLE = 600  # 10 minutes -- steady-state polling once backlog is clear


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def parse_list_unsubscribe(header_value):
    """List-Unsubscribe holds one or more comma-separated <...> targets,
    typically an https URL and/or a mailto: address. Returns (url, mailto),
    either possibly None. Multiple of the same kind (rare) keep the first."""
    url, mailto = None, None
    for target in re.findall(r"<([^>]+)>", header_value or ""):
        if target.lower().startswith("mailto:") and mailto is None:
            mailto = target[len("mailto:"):]
        elif target.lower().startswith("http") and url is None:
            url = target
    return url, mailto


def discover_mailboxes():
    conn = fetch_batch.connect()
    try:
        return [name for name, flags in list_mailboxes(conn)]
    finally:
        conn.logout()


def scheduled_mailboxes(mailboxes):
    for mailbox in mailboxes:
        yield mailbox
        if mailbox != 'INBOX' and 'INBOX' in mailboxes:
            yield 'INBOX'


def choose_uids(uids, cursor, limit):
    ordered = sorted(set(uids), key=int, reverse=True)
    newest = ordered[:limit // 2]
    older = ordered[len(newest):]
    if cursor:
        older = [uid for uid in older if int(uid) < cursor] + [uid for uid in older if int(uid) >= cursor]
    rotating = older[:limit - len(newest)]
    return newest + rotating, int(rotating[-1]) if rotating else 0


def fetch(mailbox, prefix):
    conn = fetch_batch.connect()
    try:
        return _fetch(conn, mailbox, prefix)
    finally:
        conn.logout()


def _fetch(conn, mailbox, prefix):
    typ, _ = conn.select(quote_mailbox(mailbox), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Could not select mailbox {mailbox!r}")
    uidvalidity = fetch_batch.mailbox_uidvalidity(conn)
    criteria = ["ALL"]
    for keyword in sorted(RETENTION_KEYWORDS):
        criteria.extend(["UNKEYWORD", keyword])
    typ, data = conn.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise RuntimeError("SEARCH failed")
    pending = data[0].split() if data and data[0] else []
    cursor_path = STATE_DIR / "fetch_cursors.json"
    try:
        cursors = json.loads(cursor_path.read_text())
        if not isinstance(cursors, dict):
            cursors = {}
    except (OSError, ValueError):
        cursors = {}
    saved = cursors.get(mailbox, {})
    if not isinstance(saved, dict):
        saved = {}
    cursor = saved.get("uid", 0) if saved.get("uidvalidity") == uidvalidity else 0
    if not isinstance(cursor, int) or cursor < 0:
        cursor = 0
    uids, cursor = choose_uids(pending, cursor, BATCH_SIZE)
    cursors[mailbox] = {"uidvalidity": uidvalidity, "uid": cursor}
    atomic_write(cursor_path, json.dumps(cursors) + "\n")

    in_records, env_records = [], []
    chunk = 50
    for i in range(0, len(uids), chunk):
        if len(in_records) >= BATCH_SIZE:
            break
        batch = uids[i : i + chunk]
        idset = b",".join(batch).decode()
        typ, fdata = conn.uid(
            "FETCH", idset,
            "(UID INTERNALDATE FLAGS BODY.PEEK[HEADER.FIELDS "
            "(MESSAGE-ID SUBJECT FROM DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)] BODY.PEEK[])",
        )
        if typ != "OK":
            continue
        items = [item for item in fdata if isinstance(item, tuple)]
        for j in range(0, len(items), 2):
            if len(in_records) >= BATCH_SIZE:
                break
            meta_line, header_bytes = items[j]
            _, body_bytes = items[j + 1] if j + 1 < len(items) else (None, b"")
            uid_match = re.search(rb"UID (\d+)", meta_line)
            date_match = re.search(rb'INTERNALDATE "([^"]+)"', meta_line)
            flags_match = re.search(rb"FLAGS \(([^)]*)\)", meta_line)
            uid = uid_match.group(1).decode() if uid_match else ""
            internaldate = date_match.group(1).decode() if date_match else ""
            flags = set((flags_match.group(1).decode() if flags_match else "").split())
            if flags & RETENTION_KEYWORDS:
                continue  # already classified by an earlier pipeline pass

            import email
            header_msg = email.message_from_bytes(header_bytes)
            message_id = (header_msg.get("Message-ID") or "").strip()
            if not message_id:
                if not uid:
                    raise RuntimeError("Message did not report a UID")
                message_id = fetch_batch.local_message_id(mailbox, uidvalidity, uid)

            from email.utils import parseaddr, parsedate_to_datetime
            subject = fetch_batch.decode_str(header_msg.get("Subject", ""))
            from_raw = fetch_batch.decode_str(header_msg.get("From", ""))
            display_name, from_email = parseaddr(from_raw)
            unsub_url, unsub_mailto = parse_list_unsubscribe(header_msg.get("List-Unsubscribe", ""))
            one_click = "list-unsubscribe=one-click" in header_msg.get("List-Unsubscribe-Post", "").lower()
            raw_date = header_msg.get("Date", "")
            # process_batch.py's parse_jmap() expects ISO format (JMAP's
            # native convention) -- convert from the raw RFC 2822 header.
            try:
                date = parsedate_to_datetime(raw_date).isoformat()
            except (TypeError, ValueError):
                from datetime import datetime as _dt
                date = _dt.strptime(internaldate, "%d-%b-%Y %H:%M:%S %z").isoformat()
            snippet = fetch_batch.extract_snippet(body_bytes)

            in_records.append(
                {"id": message_id, "subject": subject, "from": from_email, "date": date, "snippet": snippet}
            )
            env_records.append(
                {
                    "uid": uid,
                    "uidvalidity": uidvalidity,
                    "internaldate": internaldate,
                    "subject": subject,
                    "from_email": from_email,
                    "display_name": display_name,
                    "message_id": message_id,
                    "unsubscribe_url": unsub_url,
                    "unsubscribe_mailto": unsub_mailto,
                    "one_click": one_click,
                }
            )
    if pending and not in_records:
        raise RuntimeError("Unclassified messages remain but FETCH returned no usable records")

    Path(f"{prefix}_in.json").write_text(json.dumps(in_records, indent=1))
    Path(f"{prefix}_env.json").write_text(json.dumps(env_records, indent=1))
    return in_records


def classify_with_backend(records, backend_name):
    """Classify one list of records against one backend, respecting that
    backend's own default_concurrency. Factored out so auto mode can run
    this twice concurrently -- once per backend -- and merge the results
    back in order."""
    if not records:
        return []
    if backend_name == "openrouter-free" and not classify.free_classification_enabled():
        return classify.free_disabled_results(records)
    system_prompt = PROMPT_PATH.read_text()
    backend = classify.BACKENDS[backend_name]
    headers = {"Content-Type": "application/json", "Authorization": backend["auth_header"]()}
    concurrency = backend["default_concurrency"]

    results = [None] * len(records)
    # Deliberately not a context manager: on a hung future, .shutdown(wait=True)
    # on exit would block forever too. Overall deadline instead -- classify.py's
    # own urlopen(timeout=60) is an inactivity timeout, not a total-duration
    # cap, and a slow/streaming response can dodge it indefinitely (reproduced
    # repeatedly on this box). Anything not done by the deadline is left
    # running (orphaned) and marked as an error here so the batch still moves.
    ex = ThreadPoolExecutor(max_workers=concurrency)
    futures = {
        ex.submit(classify.classify_one, backend["url"], headers, backend["default_model"], system_prompt, rec): i
        for i, rec in enumerate(records)
    }
    deadline = max(90, len(records) * 8)
    try:
        for fut in as_completed(futures, timeout=deadline):
            try:
                results[futures[fut]] = fut.result()
            except Exception as exc:
                rec = records[futures[fut]]
                results[futures[fut]] = {"id": rec["id"], "action": "error", "reason": f"classification failed: {exc}"}
    except FutureTimeoutError:
        pass
    ex.shutdown(wait=False, cancel_futures=True)

    for i, rec in enumerate(records):
        if results[i] is None:
            log(f"  {rec['id']}: no response within {deadline}s deadline ({backend_name}), marking as error")
            results[i] = {"id": rec["id"], "action": "error", "reason": "classification timed out"}
    return results


def _classify_free_and_time(records):
    """classify_with_backend against openrouter-free, timing the wall clock so
    the result feeds mailbox_settings' rolling free-rate estimate (what the
    auto-mode escalation decision is based on)."""
    if not classify.free_classification_enabled():
        log("Free classification disabled; messages retained for retry")
        return classify.free_disabled_results(records)
    t0 = time.monotonic()
    results = classify_with_backend(records, "openrouter-free")
    mailbox_settings.record_free_batch(sum(r["action"] != "error" for r in results), time.monotonic() - t0)
    return results


def full_backlog_count(mailboxes):
    """A cheap-ish full recount: one IMAP SEARCH per mailbox for UIDs that
    don't carry any retention keyword yet -- the same signal fetch() uses to
    skip already-classified mail -- returning just a count, no header/body
    fetch. Still a real IMAP round trip per mailbox, so this is only meant to
    run periodically (see mailbox_settings.BACKLOG_REFRESH_SECONDS), not
    every batch."""
    conn = fetch_batch.connect()
    total = 0
    try:
        for mailbox in mailboxes:
            typ, _ = conn.select(quote_mailbox(mailbox), readonly=True)
            if typ != "OK":
                raise RuntimeError("Backlog count could not select a mailbox")
            criteria = []
            for kw in RETENTION_KEYWORDS:
                criteria += ["UNKEYWORD", kw]
            typ, data = conn.uid("SEARCH", None, *criteria)
            if typ != "OK":
                raise RuntimeError("Backlog count search failed")
            total += len(data[0].split()) if data and data[0] else 0
    finally:
        conn.logout()
    return total


def get_backlog_estimate():
    """Remaining-backlog estimate for the auto-mode escalation decision.
    Deliberate tradeoff: a full accurate recount only runs once per
    mailbox_settings.BACKLOG_REFRESH_SECONDS and is cached; between refreshes
    the cached number is decremented by however many messages each batch
    actually finished (see process_one_batch). So this is an estimate that
    can drift -- new mail arriving, or going stale near a refresh boundary --
    not a live truth. Acceptable here since it only feeds a rough free-vs-paid
    split, not billing or a user-facing count."""
    estimate, is_fresh = mailbox_settings.get_cached_backlog(mailbox_settings.BACKLOG_REFRESH_SECONDS)
    if not is_fresh:
        estimate = full_backlog_count(discover_mailboxes())
        mailbox_settings.set_backlog_estimate(estimate)
        log(f"backlog estimate refreshed via full IMAP scan: {estimate}")
    return estimate


PAID_RETRY_SECONDS = 300
_paid_retry_at = 0.0
FREE_RETRY_SECONDS = 300
_free_retry_at = 0.0


def log_paid_failures(failures):
    statuses, classes = {}, {}
    for result in failures:
        status = result.get('http_status')
        if type(status) is int and 100 <= status <= 599:
            statuses[str(status)] = statuses.get(str(status), 0) + 1
            kind = 'http'
        else:
            reason = str(result.get('reason', '')).lower()
            if any(word in reason for word in ('timeout', 'timed out', 'deadline')):
                kind = 'timeout'
            elif any(word in reason for word in ('json', 'expecting value', 'invalid classification', 'invalid retention', 'invalid category', 'invalid attention')):
                kind = 'invalid_response'
            else:
                kind = 'other'
        classes[kind] = classes.get(kind, 0) + 1
    log('Paid classification failures: ' + json.dumps(
        {'count': len(failures), 'http_statuses': statuses, 'error_classes': classes}, sort_keys=True))


def paid_cooldown_results(records):
    return [{'id': record['id'], 'action': 'error',
             'reason': 'Paid classification cooling down; free fallback disabled; retained for retry'}
            for record in records]


def _classify_auto(records):
    """Prefer free, temporarily routing failures to paid until a free probe succeeds."""
    global _free_retry_at
    if _free_retry_at and time.monotonic() < _free_retry_at:
        return classify_batch(records, "paid_only")
    if _free_retry_at:
        probe = _classify_free_and_time(records[:1])
        if probe[0]["action"] == "error":
            _free_retry_at = time.monotonic() + FREE_RETRY_SECONDS
            return classify_batch(records, "paid_only")
        _free_retry_at = 0.0
        free_results, paid_results = _classify_auto(records[1:]) if len(records) > 1 else ([], [])
        return probe + free_results, paid_results

    backlog = get_backlog_estimate()
    rate = mailbox_settings.recent_free_rate()
    free_count, _ = mailbox_settings.decide_backend_split(backlog, rate, len(records))
    free_records, paid_records = records[:free_count], records[free_count:]
    free_results = _classify_free_and_time(free_records) if free_records else []
    failures = {result["id"] for result in free_results if result["action"] == "error"}
    if failures:
        _free_retry_at = time.monotonic() + FREE_RETRY_SECONDS
        log(f"Free classification failed for {len(failures)} message(s); using paid temporarily; free probe in 300s")
        paid_records = [record for record in free_records if record["id"] in failures] + paid_records
        free_results = [result for result in free_results if result["id"] not in failures]
    recovered_free, paid_results = classify_batch(
        paid_records, "paid_only" if _free_retry_at else "paid") if paid_records else ([], [])
    return free_results + recovered_free, paid_results


def classify_batch(records, mode):
    """Retry paid failures on free, periodically probing paid for recovery.

    The saved mode stays unchanged. Free mode never initiates paid requests.
    Only final results are returned, so each message is applied once.
    """
    global _paid_retry_at
    if mode not in ("paid_only", "paid", "auto", "free"):
        raise ValueError("Unknown classification policy")
    if not records:
        return [], []
    if mode == "free":
        return _classify_batch(records, mode)
    if mode == "auto":
        return _classify_auto(records)
    allow_free = mode == "paid" and classify.free_classification_enabled()

    probe_results = []
    if _paid_retry_at:
        if time.monotonic() < _paid_retry_at:
            if not allow_free:
                log("Paid backend cooling down; free fallback disabled; messages retained for retry")
                return [], paid_cooldown_results(records)
            log("Paid backend cooling down; using free tier")
            return _classify_free_and_time(records), []
        log("Probing paid backend for recovery with one message")
        probe_results = classify_with_backend(records[:1], "openrouter-paid")
        if probe_results[0]["action"] == "error":
            _paid_retry_at = time.monotonic() + PAID_RETRY_SECONDS
            log_paid_failures(probe_results)
            if not allow_free:
                log("Paid probe failed; free fallback disabled; retrying paid in 300s")
                return [], probe_results + paid_cooldown_results(records[1:])
            log("Paid probe failed; using free tier and retrying paid in 300s")
            return _classify_free_and_time(records), []
        _paid_retry_at = 0.0
        log("Paid backend recovered; resuming configured mode")
        remaining = records[1:]
    else:
        remaining = records

    free_results, paid_results = _classify_batch(remaining, mode) if remaining else ([], [])
    paid_results = probe_results + paid_results
    failures = [r for r in paid_results if r["action"] == "error"]
    if failures:
        log_paid_failures(failures)
        if (any(r.get("http_status") == 402 for r in failures)
                or len(failures) / len(paid_results) >= 0.8):
            _paid_retry_at = time.monotonic() + PAID_RETRY_SECONDS
            log("Paid backend unavailable; next recovery probe in 300s")
        if not allow_free:
            log("Free fallback disabled; preserving paid errors for retry")
            return free_results, paid_results
        failed_ids = {r["id"] for r in failures}
        retry_records = [r for r in records if r["id"] in failed_ids]
        log(f"Retrying {len(retry_records)} failed paid classification(s) on free tier")
        free_results += _classify_free_and_time(retry_records)
        paid_results = [r for r in paid_results if r["id"] not in failed_ids]
    return free_results, paid_results


def _classify_batch(records, mode):
    """Returns (free_results, paid_results) -- kept separate, rather than one
    merged list, so the caller can tell a free-only quota exhaustion apart
    from a genuine paid-backend problem (see process_one_batch)."""
    if mode in ("paid", "paid_only"):
        log(f"  backend split: 0 free, {len(records)} paid (mode={mode})")
        return [], classify_with_backend(records, "openrouter-paid")

    if mode == "free":
        free_count, paid_count = len(records), 0
    else:  # auto
        backlog_estimate = get_backlog_estimate()
        free_rate = mailbox_settings.recent_free_rate()
        free_count, paid_count = mailbox_settings.decide_backend_split(backlog_estimate, free_rate, len(records))
        hours_at_free_alone = (backlog_estimate / free_rate / 3600) if free_rate > 0 else float("inf")
        log(
            f"  auto mode: backlog~{backlog_estimate} msg, free_rate~{free_rate:.4f} msg/s "
            f"(~{hours_at_free_alone:.2f}h to clear at free alone) -> split free={free_count} paid={paid_count}"
        )

    log(f"  backend split: {free_count} free, {paid_count} paid (mode={mode})")
    free_records, paid_records = records[:free_count], records[free_count:]

    if free_records and paid_records:
        with ThreadPoolExecutor(max_workers=2) as outer:
            free_future = outer.submit(_classify_free_and_time, free_records)
            paid_future = outer.submit(classify_with_backend, paid_records, "openrouter-paid")
            free_results = free_future.result()
            paid_results = paid_future.result()
    elif free_records:
        free_results, paid_results = _classify_free_and_time(free_records), []
    else:
        free_results, paid_results = [], classify_with_backend(paid_records, "openrouter-paid")

    return free_results, paid_results


def delete_pending_trash(mailbox):
    conn = retention_sweep.connect()
    try:
        found, deleted = retention_sweep.sweep_trash(conn, mailbox)
        if found != deleted:
            raise RuntimeError("Trash deletion incomplete; pending tags retained for retry")
        if deleted:
            log(f"{mailbox}: deleted {deleted} message(s) classified as trash")
    finally:
        conn.logout()


def process_one_batch(mailbox):
    started = time.monotonic()
    prefix = str(STATE_DIR / "current_batch")
    runtime_status.write_status("fetching", mailbox=mailbox)
    trash_error = None
    try:
        delete_pending_trash(mailbox)
    except Exception as exc:
        trash_error = str(exc)
        log(f"{mailbox}: trash cleanup failed; will retry: {exc!r}")
        runtime_status.write_status("error", error=trash_error[:200])
    records = fetch(mailbox, prefix)
    fetched = time.monotonic()
    if not records:
        return "error" if trash_error else "empty"

    mode = mailbox_settings.get_classify_mode()
    log(f"{mailbox}: classify_mode={mode}, classifying {len(records)} message(s)")
    runtime_status.write_status("classifying", mode=mode, batch_size=len(records))
    free_results, paid_results = classify_batch(records, mode)
    classified = time.monotonic()
    results = free_results + paid_results
    Path(f"{prefix}_out.json").write_text(json.dumps(results, indent=1))
    counts = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    log(f"classified: {counts}")

    old_argv = sys.argv
    try:
        runtime_status.write_status("applying")
        sys.argv = ["process_batch.py", prefix, mailbox]
        operation_ids = process_batch.main()
    finally:
        sys.argv = old_argv

    ops_path = Path(f"{prefix}_ops.json")
    ops = json.loads(ops_path.read_text()) if ops_path.exists() else []
    applied = keyword_tool.apply_ops(ops)
    applied_ids = {operation_ids[mid] for mid in applied["applied"] if mid in operation_ids}
    for operation in ops:
        if operation.get("sample_sender") and operation["message_id"] in applied["applied"]:
            process_batch.tahor_db.record_sender_sample(operation["sample_sender"], operation["message_id"])

    # Only mark real classifications as done -- an "error" result (e.g. a
    # rate-limited request that exhausted its retries) should be retried in
    # a later batch, not silently skipped forever.
    errored_ids = {r["id"] for r in results if r["action"] == "error"}
    if errored_ids:
        log(f"{mailbox}: {len(errored_ids)} message(s) errored, will retry next pass")
    with open(PROCESSED_IDS_PATH, "a") as f:
        for r in records:
            if r["id"] in applied_ids:
                f.write(r["id"] + "\n")

    mailbox_settings.decrement_backlog_estimate(len(applied_ids))
    log(f"{mailbox}: {len(applied_ids)} applied, {len(records) - len(applied_ids)} left for retry")
    fields = {"last_batch_applied": len(applied_ids), "last_batch_pending": len(records) - len(applied_ids)}
    if applied_ids:
        fields["last_success_at"] = datetime.now(timezone.utc).isoformat()
    runtime_status.write_status("processed" if applied_ids else "error", **fields)
    finished = time.monotonic()
    log(f"batch timing: fetch={fetched-started:.2f}s classify={classified-fetched:.2f}s "
        f"apply={finished-classified:.2f}s total={finished-started:.2f}s "
        f"applied={len(applied_ids)} pending={len(records)-len(applied_ids)}")

    # Judge final outcomes after fallback, including batches smaller than ten.
    # Any successful work keeps the normal cadence; a total outage gets a
    # bounded retry instead of the old two-hour sleep.
    if not applied_ids and any(r["action"] != "error" for r in results):
        return "error"
    if (paid_results and not free_results and not errored_ids and not trash_error
            and len(applied_ids) == len(records)):
        return "processed_paid"
    return batch_status(results)


def batch_status(results):
    return "backend_unavailable" if results and all(r["action"] == "error" for r in results) else "processed"


BACKEND_RETRY_SECONDS = 300


def main():
    process_batch.tahor_db.init_db()
    PROCESSED_IDS_PATH.touch(exist_ok=True)
    log("backlog_worker starting (in-process mode)")

    while True:
        # "empty" (genuinely no unprocessed mail left) is the only status
        # that should trigger the idle sleep below -- "backend_unavailable" and
        # "error" mean real mail is still waiting, just blocked, and should
        # retry right after their own backoff instead of also being logged
        # as "no new mail" and sleeping an extra SLEEP_WHEN_IDLE on top.
        all_empty = True
        try:
            mailboxes = discover_mailboxes()
        except Exception as e:
            log(f"Mailbox discovery failed: {e!r}; retrying")
            runtime_status.write_status("error", error=str(e)[:200])
            time.sleep(SLEEP_BETWEEN_BATCHES)
            continue
        runtime_status.write_status("fetching", mailbox_count=len(mailboxes))
        for mailbox in scheduled_mailboxes(mailboxes):
            try:
                status = process_one_batch(mailbox)
            except Exception as e:
                log(f"{mailbox}: exception {e!r}, backing off")
                status = "error"
                runtime_status.write_status("error", error=str(e)[:200])

            if status != "empty":
                all_empty = False

            if status == "processed":
                time.sleep(SLEEP_BETWEEN_BATCHES)
            elif status == "processed_paid":
                time.sleep(PAID_BATCH_DELAY)
            elif status == "backend_unavailable":
                log(f"{mailbox}: no classifications succeeded -- retrying in {BACKEND_RETRY_SECONDS}s")
                runtime_status.write_status("retrying", retry_seconds=BACKEND_RETRY_SECONDS)
                time.sleep(BACKEND_RETRY_SECONDS)
            elif status == "error":
                time.sleep(SLEEP_BETWEEN_BATCHES)

        if all_empty:
            log(f"No new mail in any mailbox -- sleeping {SLEEP_WHEN_IDLE}s")
            runtime_status.write_status("idle")
            time.sleep(SLEEP_WHEN_IDLE)


if __name__ == "__main__":
    main()
