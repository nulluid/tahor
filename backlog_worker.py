#!/usr/bin/env python3
"""
Continuously classify unprocessed mail, running unattended.

Loop: for each mailbox in MAILBOXES, fetch a batch of not-yet-processed
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

# PROMPT_PATH can point anywhere, including a separate private repo, if you
# want your prompt/config to have its own tracked history -- it's just an
# env var, not a hardcoded assumption. Defaults to this repo's own
# gitignored prompt.txt (see config.py's vendor_buckets() for the same
# convention).
PROMPT_PATH = Path(os.environ.get("PROMPT_PATH", REPO / "prompt.txt"))
PROCESSED_IDS_PATH = REPO / "processed_message_ids.txt"
LOG_PATH = REPO / "logs" / "backlog_worker.log"

# IMAP folders this worker watches. Edit for your own mailbox layout --
# add any folder besides INBOX you want it to keep working through.
MAILBOXES = ["INBOX"]
RETENTION_KEYWORDS = {"retention-forever", "retention-standard", "retention-transient", "retention-pending-review"}
BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", 50))
# Concurrency is no longer a single flat setting here -- each backend runs at
# its own classify.BACKENDS[...]["default_concurrency"] (see classify.py):
# 2 for the free tiers, 20 for openrouter-paid (there's no daily cap to be
# gentle with there, only the account's real rate limit and your box's own
# capacity should bound it -- see mailbox_settings.py's comment on how that
# 20 was actually measured, not guessed).
SLEEP_BETWEEN_BATCHES = int(os.environ.get("WORKER_SLEEP_BETWEEN_BATCHES", 45))  # was tuned for a free-tier's RPM limit
SLEEP_WHEN_IDLE = 600  # 10 minutes -- steady-state polling once backlog is clear


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def fetch(mailbox, prefix):
    conn = fetch_batch.connect()
    try:
        with open(PROCESSED_IDS_PATH) as f:
            processed = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        processed = set()

    typ, _ = conn.select(f'"{mailbox}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Could not select mailbox {mailbox!r}")
    typ, data = conn.search(None, "ALL")
    if typ != "OK":
        raise RuntimeError("SEARCH failed")
    uids = data[0].split()

    in_records, env_records = [], []
    chunk = 50
    for i in range(0, len(uids), chunk):
        if len(in_records) >= BATCH_SIZE:
            break
        batch = uids[i : i + chunk]
        idset = b",".join(batch).decode()
        typ, fdata = conn.fetch(
            idset, "(UID INTERNALDATE FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)] BODY.PEEK[])"
        )
        if typ != "OK":
            continue
        items = [item for item in fdata if isinstance(item, tuple)]
        for j in range(0, len(items), 2):
            if len(in_records) >= BATCH_SIZE:
                break
            meta_line, header_bytes = items[j]
            _, body_bytes = items[j + 1] if j + 1 < len(items) else (None, b"")
            import re
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
            if not message_id or message_id in processed:
                continue

            from email.utils import parseaddr, parsedate_to_datetime
            subject = fetch_batch.decode_str(header_msg.get("Subject", ""))
            from_raw = fetch_batch.decode_str(header_msg.get("From", ""))
            _, from_email = parseaddr(from_raw)
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
                {"uid": uid, "internaldate": internaldate, "subject": subject, "from_email": from_email, "message_id": message_id}
            )
    conn.logout()

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
            results[futures[fut]] = fut.result()
    except FutureTimeoutError:
        pass
    ex.shutdown(wait=False)

    for i, rec in enumerate(records):
        if results[i] is None:
            log(f"  {rec['id']}: no response within {deadline}s deadline ({backend_name}), marking as error")
            results[i] = {"id": rec["id"], "action": "error", "reason": "classification timed out"}
    return results


def _classify_free_and_time(records):
    """classify_with_backend against openrouter-free, timing the wall clock so
    the result feeds mailbox_settings' rolling free-rate estimate (what the
    auto-mode escalation decision is based on)."""
    t0 = time.monotonic()
    results = classify_with_backend(records, "openrouter-free")
    mailbox_settings.record_free_batch(len(records), time.monotonic() - t0)
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
            typ, _ = conn.select(f'"{mailbox}"', readonly=True)
            if typ != "OK":
                continue
            criteria = []
            for kw in RETENTION_KEYWORDS:
                criteria += ["UNKEYWORD", kw]
            typ, data = conn.search(None, *criteria)
            if typ == "OK":
                total += len(data[0].split())
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
        estimate = full_backlog_count(MAILBOXES)
        mailbox_settings.set_backlog_estimate(estimate)
        log(f"backlog estimate refreshed via full IMAP scan: {estimate}")
    return estimate


def classify_batch(records, mode):
    """Returns (free_results, paid_results) -- kept separate, rather than one
    merged list, so the caller can tell a free-only quota exhaustion apart
    from a genuine paid-backend problem (see process_one_batch)."""
    if mode == "paid":
        log(f"  backend split: 0 free, {len(records)} paid (mode=paid)")
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


def process_one_batch(mailbox):
    prefix = str(REPO / "current_batch")
    records = fetch(mailbox, prefix)
    if not records:
        return "empty"

    mode = mailbox_settings.get_classify_mode()
    log(f"{mailbox}: classify_mode={mode}, classifying {len(records)} message(s)")
    free_results, paid_results = classify_batch(records, mode)
    results = free_results + paid_results
    Path(f"{prefix}_out.json").write_text(json.dumps(results, indent=1))
    counts = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    log(f"classified: {counts}")

    old_argv = sys.argv
    try:
        sys.argv = ["process_batch.py", "current_batch", mailbox]
        os.chdir(REPO)
        process_batch.main()
    finally:
        sys.argv = old_argv

    ops_path = Path(f"{prefix}_ops.json")
    ops = json.loads(ops_path.read_text()) if ops_path.exists() else []
    if ops:
        old_argv = sys.argv
        try:
            sys.argv = ["keyword_tool.py", f"{prefix}_ops.json"]
            keyword_tool.main()
        finally:
            sys.argv = old_argv

    trash_ids = json.loads(Path(f"{prefix}_trash_ids.json").read_text())
    if trash_ids:
        log(f"{mailbox}: {len(trash_ids)} message(s) marked for trash -- NOT deleted (handled by retention_sweep.py separately)")

    # Only mark real classifications as done -- an "error" result (e.g. a
    # rate-limited request that exhausted its retries) should be retried in
    # a later batch, not silently skipped forever.
    errored_ids = {r["id"] for r in results if r["action"] == "error"}
    if errored_ids:
        log(f"{mailbox}: {len(errored_ids)} message(s) errored, will retry next pass")
    with open(PROCESSED_IDS_PATH, "a") as f:
        for r in records:
            if r["id"] not in errored_ids:
                f.write(r["id"] + "\n")

    mailbox_settings.decrement_backlog_estimate(len(records) - len(errored_ids))

    # A near-total error rate on a real-sized batch almost always means a
    # backend's quota/capacity is exhausted, not a handful of transient
    # failures -- worth backing off instead of hammering it every 90s. But
    # that check has to be per-backend: in auto mode a batch is a free/paid
    # split, and free running out (which happens daily, on schedule) must
    # never look like "everything is exhausted" and put the whole worker
    # to sleep for 2 hours -- paid has no daily cap and should just keep
    # going. Only a backend that was actually *used* in this batch and
    # came back mostly errors counts.
    def exhausted(subset):
        return len(subset) >= 10 and sum(1 for r in subset if r["action"] == "error") / len(subset) >= 0.8

    free_exhausted = exhausted(free_results)
    paid_exhausted = exhausted(paid_results)

    if paid_results and not paid_exhausted:
        if free_exhausted:
            log(f"{mailbox}: free tier looks exhausted mid-batch -- staying on paid, no worker-wide backoff")
        return "processed"
    if free_exhausted or paid_exhausted:
        return "quota_exhausted"
    return "processed"


QUOTA_BACKOFF_SECONDS = 2 * 60 * 60  # 2 hours -- self-correcting even without knowing the exact daily reset time


def main():
    PROCESSED_IDS_PATH.touch(exist_ok=True)
    log("backlog_worker starting (in-process mode)")

    while True:
        # "empty" (genuinely no unprocessed mail left) is the only status
        # that should trigger the idle sleep below -- "quota_exhausted" and
        # "error" mean real mail is still waiting, just blocked, and should
        # retry right after their own backoff instead of also being logged
        # as "no new mail" and sleeping an extra SLEEP_WHEN_IDLE on top.
        all_empty = True
        for mailbox in MAILBOXES:
            try:
                status = process_one_batch(mailbox)
            except Exception as e:
                log(f"{mailbox}: exception {e!r}, backing off")
                status = "error"

            if status != "empty":
                all_empty = False

            if status == "processed":
                time.sleep(SLEEP_BETWEEN_BATCHES)
            elif status == "quota_exhausted":
                log(f"{mailbox}: quota looks exhausted (>=80% error rate) -- sleeping {QUOTA_BACKOFF_SECONDS}s")
                time.sleep(QUOTA_BACKOFF_SECONDS)
            elif status == "error":
                time.sleep(SLEEP_BETWEEN_BATCHES)

        if all_empty:
            log(f"No new mail in any mailbox -- sleeping {SLEEP_WHEN_IDLE}s")
            time.sleep(SLEEP_WHEN_IDLE)


if __name__ == "__main__":
    main()
