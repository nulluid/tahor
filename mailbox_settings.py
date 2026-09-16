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
import copy
import fcntl
import tempfile
from functools import wraps
import os
from datetime import datetime, timezone
from pathlib import Path

# Fixed path, not Path(__file__).parent -- worker and web app run from different directories.
SETTINGS_PATH = Path(os.environ.get("TAHOR_SETTINGS_PATH", Path.home() / ".config" / "tahor" / "settings.json"))

MODES = ("free", "paid", "auto")

# Rule drafting is rare and judgment-heavy, so it's worth a stronger model than routine classification uses.
RULE_MODELS = {
    "nemotron-free": {
        "label": "Nemotron 3 Super — free tier",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "auth_env": "OPENROUTER_API_KEY",
    },
    "gemini-flash": {
        "label": "Gemini 3.6 Flash — fast, effectively free",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3.6-flash",
        "auth_env": "GEMINI_API_KEY",
    },
    "gemini-pro": {
        "label": "Gemini 3 Pro — more capable, still cheap",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3-pro",
        "auth_env": "GEMINI_API_KEY",
    },
    "claude-opus": {
        "label": "Claude Opus 5 (via OpenRouter) — best judgment, highest cost",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "anthropic/claude-opus-5",
        "auth_env": "OPENROUTER_API_KEY",
    },
    "gpt5": {
        "label": "GPT-5.1 (via OpenRouter) — strong alternative",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openai/gpt-5.1",
        "auth_env": "OPENROUTER_API_KEY",
    },
}
DEFAULT_RULE_MODEL = "nemotron-free"

# Reply drafting runs more often than rule drafting (once per matching
# email, not a few times a month) and the whole point is prose quality --
# a separate model choice from RULE_MODELS, defaulting to a free option so
# a zero-cost setup is possible out of the box.
REPLY_MODELS = {
    "gemini-flash": {
        "label": "Gemini 3.6 Flash — free, solid everyday English",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3.6-flash",
        "auth_env": "GEMINI_API_KEY",
    },
    "nemotron-free": {
        "label": "Nemotron 3 Super (via OpenRouter, free tier)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "auth_env": "OPENROUTER_API_KEY",
    },
    "euryale-70b": {
        "label": "Euryale 70B (via OpenRouter) — prose quality, cheap at low volume",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "sao10k/l3.3-euryale-70b",
        "auth_env": "OPENROUTER_API_KEY",
    },
    "claude-opus": {
        "label": "Claude Opus 5 (via OpenRouter) — best writing, highest cost",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "anthropic/claude-opus-5",
        "auth_env": "OPENROUTER_API_KEY",
    },
    "gpt5": {
        "label": "GPT-5.1 (via OpenRouter) — strong alternative",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openai/gpt-5.1",
        "auth_env": "OPENROUTER_API_KEY",
    },
}
DEFAULT_REPLY_MODEL = "nemotron-free"

DEFAULT_SETTINGS = {
    "classify_mode": "free",
    "rule_model": DEFAULT_RULE_MODEL,
    "reply_model": DEFAULT_REPLY_MODEL,
    "free_rate_log": [],  # rolling [{"messages": N, "seconds": S}, ...], see record_free_batch
    "backlog_estimate": None,
    "backlog_estimate_at": None,
    "reply_triggers": [],  # [{"type": "sender_email"|"sender_domain", "value": str}, ...]
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

# Initial throughput estimate, measured end-to-end at concurrency 20.
PAID_CONCURRENCY = 20
PAID_MSG_LATENCY_SECONDS = 2.06
PAID_RATE_MSGS_PER_SEC = 1 / PAID_MSG_LATENCY_SECONDS  # measured aggregate seconds per message

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
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(DEFAULT_SETTINGS)
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        data = {}
    merged.update(data)
    if merged.get("classify_mode") not in MODES:
        merged["classify_mode"] = "free"
    if merged.get("rule_model") not in RULE_MODELS:
        merged["rule_model"] = DEFAULT_RULE_MODEL
    if merged.get("reply_model") not in REPLY_MODELS:
        merged["reply_model"] = DEFAULT_REPLY_MODEL
    return merged


def locked_update(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS_PATH.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return function(*args, **kwargs)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return wrapped


def save_settings(settings):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=SETTINGS_PATH.parent, prefix=".settings-", delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(settings, stream, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(SETTINGS_PATH)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def get_classify_mode():
    return load_settings().get("classify_mode", "free")


@locked_update
def set_classify_mode(mode):
    if mode not in MODES:
        raise ValueError(f"Unknown classify_mode {mode!r}, choose from {MODES}")
    settings = load_settings()
    settings["classify_mode"] = mode
    save_settings(settings)


def get_rule_model():
    return load_settings().get("rule_model", DEFAULT_RULE_MODEL)


@locked_update
def set_rule_model(key):
    if key not in RULE_MODELS:
        raise ValueError(f"Unknown rule_model {key!r}, choose from {tuple(RULE_MODELS)}")
    settings = load_settings()
    settings["rule_model"] = key
    save_settings(settings)


def get_reply_model():
    return load_settings().get("reply_model", DEFAULT_REPLY_MODEL)


@locked_update
def set_reply_model(key):
    if key not in REPLY_MODELS:
        raise ValueError(f"Unknown reply_model {key!r}, choose from {tuple(REPLY_MODELS)}")
    settings = load_settings()
    settings["reply_model"] = key
    save_settings(settings)


TRIGGER_TYPES = ("sender_email", "sender_domain")


def get_reply_triggers():
    return load_settings().get("reply_triggers", [])


@locked_update
def add_reply_trigger(trigger_type, value):
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError(f"Unknown trigger type {trigger_type!r}, choose from {TRIGGER_TYPES}")
    value = value.strip().lower()
    import re
    domain = value.rsplit("@", 1)[-1]
    labels = domain.split(".")
    if (len(labels) < 2 or len(domain) > 253
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            or (trigger_type == "sender_email" and not re.fullmatch(r"[^\s<>@\"\\]+@[^@]+", value))
            or (trigger_type == "sender_domain" and "@" in value)):
        raise ValueError("Enter a valid email address or domain.")
    settings = load_settings()
    triggers = settings.get("reply_triggers", [])
    if not any(t["type"] == trigger_type and t["value"] == value for t in triggers):
        triggers.append({"type": trigger_type, "value": value})
        settings["reply_triggers"] = triggers
        save_settings(settings)


@locked_update
def remove_reply_trigger(trigger_type, value):
    settings = load_settings()
    triggers = settings.get("reply_triggers", [])
    settings["reply_triggers"] = [t for t in triggers if not (t["type"] == trigger_type and t["value"] == value)]
    save_settings(settings)


def matches_reply_trigger(sender_email):
    sender_email = (sender_email or "").lower()
    domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""
    for t in get_reply_triggers():
        if t["type"] == "sender_email" and t["value"] == sender_email:
            return True
        if t["type"] == "sender_domain" and t["value"] == domain:
            return True
    return False


@locked_update
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
    except (ValueError, TypeError):
        return None, False
    return estimate, age <= max_age_seconds


@locked_update
def set_backlog_estimate(count):
    settings = load_settings()
    settings["backlog_estimate"] = count
    settings["backlog_estimate_at"] = datetime.now(timezone.utc).isoformat()
    save_settings(settings)


@locked_update
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


def get_inbox_grace_days():
    settings = load_settings()
    result = {}
    for status, default in (("read", 3), ("unread", 7)):
        value = settings.get(f"inbox_{status}_days", os.environ.get(f"FILING_{status.upper()}_MIN_AGE_DAYS", default))
        try:
            days = int(value)
        except (TypeError, ValueError):
            days = default
        result[status] = days if 0 <= days <= 3650 else default
    return result


@locked_update
def set_inbox_grace_days(read_days, unread_days):
    values = {}
    for status, raw in (("read", read_days), ("unread", unread_days)):
        if not str(raw).isascii() or not str(raw).isdigit() or not 0 <= int(raw) <= 3650:
            raise ValueError("Enter a whole number of days between 0 and 3650.")
        values[f"inbox_{status}_days"] = int(raw)
    settings = load_settings()
    settings.update(values)
    save_settings(settings)
