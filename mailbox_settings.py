#!/usr/bin/env python3
"""
Shared settings for Tahor's classification pipeline: the classify_mode
(free/paid/auto) and the bits of state auto mode's escalation decision
needs -- a rolling estimate of the free tier's real achievable rate, and a
cached backlog-size estimate. One JSON file (settings.json, a sibling of
decisions.db), one module, imported by both app.py (writes classify_mode
from the settings page) and backlog_worker.py (reads it every batch) so
there's a single source of truth for the file format and defaults.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# NOT Path(__file__).parent -- this module is deployed as separate file
# copies in more than one directory (the worker runs from ~/mailbox-sweep,
# the web app from ~/mailbox-decisions), and app.py/backlog_worker.py must
# agree on exactly one settings.json or a toggle in the UI silently does
# nothing to the worker. One fixed, account-wide location instead, still
# overridable for tests/other setups.
SETTINGS_PATH = Path(os.environ.get("TAHOR_SETTINGS_PATH", Path.home() / ".config" / "tahor" / "settings.json"))
SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

MODES = ("free", "paid", "auto")

DEFAULT_SETTINGS = {
    "classify_mode": "free",
    # Rolling log of recent free-tier batch results, oldest first:
    # [{"messages": N, "seconds": S}, ...] -- see record_free_batch/recent_free_rate.
    "free_rate_log": [],
    # Cached full-mailbox backlog count + when it was taken, so auto mode
    # doesn't need a full IMAP scan every batch -- see get_cached_backlog.
    "backlog_estimate": None,
    "backlog_estimate_at": None,
}

FREE_RATE_LOG_MAX = 5
# Fallback when there's no recent free-tier timing data (fresh restart, or
# free was quota-exhausted last time and produced no timing data): this
# project's measured history is roughly 50 messages per ~4 minutes when the
# free tier isn't quota-exhausted.
DEFAULT_FREE_RATE = 50 / (4 * 60)  # ~0.208 msg/sec

# How long a cached backlog estimate is trusted before a fresh full IMAP
# recount is worth its cost.
BACKLOG_REFRESH_SECONDS = 15 * 60

# Empirically measured on this box (2026-09-14): concurrency 20 against
# openrouter-paid sustained ~2.06s/message end-to-end with zero errors;
# concurrency 40 gave zero additional throughput, confirming 20 is
# OpenRouter's own ceiling here, not this box's. Throughput = concurrency /
# per-message latency. Keep PAID_CONCURRENCY in sync with classify.py's
# openrouter-paid default_concurrency if that ever changes.
PAID_CONCURRENCY = 20
PAID_MSG_LATENCY_SECONDS = 2.06
PAID_RATE_MSGS_PER_SEC = PAID_CONCURRENCY / PAID_MSG_LATENCY_SECONDS  # ~9.71 msg/sec

# Roughly $0.0003/email at current OpenRouter pricing for the paid backend's
# model at this project's snippet size -- used only for the settings page's
# "~$/hour" display, never for anything billed.
COST_PER_PAID_MSG = 0.0003

# The backlog-clear-time threshold auto mode escalates around.
ESCALATION_TARGET_SECONDS = 3600

# Batch size used only for the settings page's illustrative split/cost
# projection (mirrors backlog_worker.py's own default WORKER_BATCH_SIZE).
DISPLAY_BATCH_SIZE = 50


def load_settings():
    if not SETTINGS_PATH.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    if merged.get("classify_mode") not in MODES:
        merged["classify_mode"] = "free"
    return merged


def save_settings(settings):
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=1))
    tmp.replace(SETTINGS_PATH)


def get_classify_mode():
    return load_settings().get("classify_mode", "free")


def set_classify_mode(mode):
    if mode not in MODES:
        raise ValueError(f"Unknown classify_mode {mode!r}, choose from {MODES}")
    settings = load_settings()
    settings["classify_mode"] = mode
    save_settings(settings)


def record_free_batch(messages, seconds):
    """Append one completed openrouter-free batch's (messages, wall_clock_seconds)
    to the rolling log, trimmed to the last FREE_RATE_LOG_MAX entries."""
    if messages <= 0 or seconds <= 0:
        return
    settings = load_settings()
    log = settings.get("free_rate_log", [])
    log.append({"messages": messages, "seconds": seconds})
    settings["free_rate_log"] = log[-FREE_RATE_LOG_MAX:]
    save_settings(settings)


def recent_free_rate():
    """Average messages/second across the recent free-tier batch log, falling
    back to DEFAULT_FREE_RATE when there's no recent data."""
    log = load_settings().get("free_rate_log", [])
    if not log:
        return DEFAULT_FREE_RATE
    total_messages = sum(e["messages"] for e in log)
    total_seconds = sum(e["seconds"] for e in log)
    if total_seconds <= 0:
        return DEFAULT_FREE_RATE
    return total_messages / total_seconds


def get_cached_backlog(max_age_seconds):
    """Return (estimate, is_fresh). is_fresh is False when there's no cached
    estimate yet or it's older than max_age_seconds -- the caller should then
    do a real recount."""
    settings = load_settings()
    estimate = settings.get("backlog_estimate")
    at = settings.get("backlog_estimate_at")
    if estimate is None or at is None:
        return None, False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds()
    except ValueError:
        return None, False
    return estimate, age <= max_age_seconds


def set_backlog_estimate(count):
    settings = load_settings()
    settings["backlog_estimate"] = count
    settings["backlog_estimate_at"] = datetime.now(timezone.utc).isoformat()
    save_settings(settings)


def decrement_backlog_estimate(processed_count):
    if processed_count <= 0:
        return
    settings = load_settings()
    current = settings.get("backlog_estimate")
    if current is None:
        return
    settings["backlog_estimate"] = max(0, current - processed_count)
    save_settings(settings)


def decide_backend_split(remaining_count, recent_free_rate_msgs_per_sec, batch_size, target_seconds=ESCALATION_TARGET_SECONDS):
    """
    Decide how many of the next `batch_size` records go to the free backend
    vs. the paid backend in "auto" mode.

    This isn't "how fast can this one batch go" -- it's "what's the least
    paid capacity that keeps the *whole remaining backlog* on track to clear
    inside target_seconds". The math is done at the backlog level and then
    applied as a fraction to this one batch:

      1. If the backlog would already clear inside target_seconds running
         100% free, stay 100% free -- no reason to spend money.
      2. Otherwise, free and paid will run concurrently (two separate
         ThreadPoolExecutors), so their rates add: a fraction p of the
         backlog handled at the paid rate and (1-p) at the free rate clears
         the backlog in
             remaining_count / (free_rate + p * (paid_rate - free_rate))
         Solve that for p at target_seconds:
             p = (remaining_count / target_seconds - free_rate) / (paid_rate - free_rate)
         and clamp to [0, 1]. Apply p to this batch's size.

    Not a perfect solver -- batch-size rounding and using this batch's split
    as a stand-in for "ongoing rate" are both approximations -- but it
    reliably escalates further as the backlog-vs-1-hour gap grows, and backs
    off toward all-free as that gap closes or clears.
    """
    if remaining_count <= 0 or batch_size <= 0:
        return batch_size, 0
    free_rate = recent_free_rate_msgs_per_sec if recent_free_rate_msgs_per_sec > 0 else DEFAULT_FREE_RATE

    projected_seconds_if_all_free = remaining_count / free_rate
    if projected_seconds_if_all_free <= target_seconds:
        return batch_size, 0

    required_combined_rate = remaining_count / target_seconds
    rate_gap = PAID_RATE_MSGS_PER_SEC - free_rate
    if rate_gap <= 0:
        # Paid isn't meaningfully faster than free right now (shouldn't
        # happen in practice) -- paid is still the best available, use it.
        paid_fraction = 1.0
    else:
        paid_fraction = (required_combined_rate - free_rate) / rate_gap
    paid_fraction = max(0.0, min(1.0, paid_fraction))

    paid_count = max(0, min(batch_size, round(batch_size * paid_fraction)))
    free_count = batch_size - paid_count
    return free_count, paid_count
