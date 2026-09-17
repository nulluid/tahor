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
import math
import copy
import fcntl
import tempfile
from functools import wraps
import os
from datetime import datetime, timezone
from pathlib import Path

# Fixed path, not Path(__file__).parent -- worker and web app run from different directories.
SETTINGS_PATH = Path(os.environ.get("TAHOR_SETTINGS_PATH", Path.home() / ".config" / "tahor" / "settings.json"))

MODES = ("paid_only", "paid", "auto", "free")
AI_TASKS = ("classification", "reply", "rule", "subscriptions")

# Rule drafting is rare and judgment-heavy, so it's worth a stronger model than routine classification uses.
RULE_MODELS = {
    "none": {"label": "Disabled — choose a private model to enable", "model": "none", "url": "", "auth_env": ""},
    "grok-4.6": {
        "label": "Grok 4.6 (via OpenRouter) — rule drafting with a ZDR route",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "x-ai/grok-4.6",
        "auth_env": "OPENROUTER_API_KEY",
        "request_options": {
            "reasoning": {"effort": "low"},
            "max_tokens": 4096,
            "provider": {"only": ["xai/zdr"], "allow_fallbacks": False,
                         "zdr": True, "data_collection": "deny"},
        },
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
DEFAULT_RULE_MODEL = "none"

# Reply drafting runs more often than rule drafting (once per matching
# email, not a few times a month) and the whole point is prose quality --
# a separate model choice from RULE_MODELS. Disabled until explicitly selected.
REPLY_MODELS = {
    "none": {"label": "Disabled — choose a private model to enable", "model": "none", "url": "", "auth_env": ""},
    "grok-4.6": {
        "label": "Grok 4.6 (via OpenRouter) — premium reply writing",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "x-ai/grok-4.6",
        "auth_env": "OPENROUTER_API_KEY",
        "request_options": {
            "reasoning": {"effort": "low"},
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
            "provider": {"only": ["xai/zdr"], "allow_fallbacks": False, "zdr": True, "data_collection": "deny"},
        },
    },
    "euryale-70b": {
        "label": "Euryale 70B (via OpenRouter) — prose quality, cheap at low volume",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "sao10k/l3.3-euryale-70b",
        "auth_env": "OPENROUTER_API_KEY",
    },
    "ling-free": {
        "label": "Ling 3.0 Flash VL (via OpenRouter, free Novita ZDR route)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "inclusionai/ling-3.0-flash-vl:free",
        "auth_env": "OPENROUTER_API_KEY",
        "request_options": {
            "reasoning": {"enabled": False},
            "provider": {
                "only": ["novita"],
                "allow_fallbacks": False,
                "zdr": True,
                "data_collection": "deny",
                "max_price": {"prompt": 0, "completion": 0},
            },
        },
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
    "gpt5-flex": {
        "label": "GPT-5.1 Flex (via OpenRouter) — half-price tokens, availability varies",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openai/gpt-5.1",
        "auth_env": "OPENROUTER_API_KEY",
        "request_options": {
            "service_tier": "flex",
            "provider": {"only": ["openai/flex"], "allow_fallbacks": False},
            "reasoning": {"effort": "none"},
            "response_format": {"type": "json_object"},
        },
        "expected_service_tier": "flex",
    },
}
DEFAULT_REPLY_MODEL = "none"
# A free rule writer is a separate, reviewed choice; its provider constraints
# match the independently configured reply writer.
RULE_MODELS['ling-free'] = copy.deepcopy(REPLY_MODELS['ling-free'])
RULE_MODELS['ling-free']['request_options']['max_tokens'] = 4096


SUBSCRIPTION_MODELS = {key: copy.deepcopy(RULE_MODELS[key]) for key in ("none", "grok-4.6", "gpt5", "ling-free")}
for backend in SUBSCRIPTION_MODELS.values():
    if backend["model"] != "none":
        options = backend.setdefault("request_options", {})
        options["max_tokens"] = 4096
        # Novita Ling rejects structured-output mode; validate its JSON text locally.
        if not backend["model"].endswith(":free"):
            options["response_format"] = {"type": "json_object"}

def ai_model_registry(task):
    return {"reply": REPLY_MODELS, "rule": RULE_MODELS, "subscriptions": SUBSCRIPTION_MODELS}[task]


DEFAULT_SETTINGS = {
    "classify_mode": "free",
    "rule_model": DEFAULT_RULE_MODEL,
    "reply_model": DEFAULT_REPLY_MODEL,
    "reply_backup_model": "none",
    "subscriptions_model": "grok-4.6",
    "subscriptions_free_model": "ling-free",
    "subscriptions_ai_policy": "paid",
    "subscriptions_batch_size": 50,
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

# Initial paid throughput estimate follows the default request-start pacing.
# Real throughput varies with latency and retries; it is a planning estimate.
PAID_RATE_MSGS_PER_SEC = 1 / 3

# The backlog-clear-time threshold auto mode escalates around.
ESCALATION_TARGET_SECONDS = 4 * 3600

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


def get_ai_policy(task):
    if task not in AI_TASKS:
        raise ValueError('Unknown AI task')
    settings = load_settings()
    if task == 'classification':
        return settings['classify_mode']
    value = settings.get(task + '_ai_policy')
    if value in MODES:
        return value
    # Preserve a legacy free primary or explicitly selected free reply backup.
    registry = ai_model_registry(task)
    primary = settings.get(task + '_model', 'none')
    if registry.get(primary, {}).get('model', '').endswith(':free'):
        return 'free'
    if task == 'reply' and settings.get('reply_backup_model') in free_reply_models():
        return 'paid'
    return 'paid_only'


def get_ai_models(task):
    if task not in AI_TASKS:
        raise ValueError('Unknown AI task')
    if task == 'classification':
        return {'paid': 'openrouter-paid', 'free': 'openrouter-free'}
    settings = load_settings()
    registry = ai_model_registry(task)
    primary = settings.get(task + '_model', 'none')
    legacy_free = registry.get(primary, {}).get('model', '').endswith(':free')
    free_key = 'reply_backup_model' if task == 'reply' else task + '_free_model'
    free = settings.get(free_key, 'ling-free')
    if not registry.get(free, {}).get('model', '').endswith(':free'):
        free = primary if legacy_free else 'ling-free'
    return {'paid': 'grok-4.6' if legacy_free else primary, 'free': free}


def is_ai_enabled(task):
    if task not in AI_TASKS:
        raise ValueError('Unknown AI task')
    return task == 'classification' or load_settings().get(task + '_model', 'none') != 'none'


@locked_update
def set_ai_task_settings(task, policy, paid_model=None, free_model=None, batch_size=None, guidance=None):
    if task not in AI_TASKS or policy not in MODES:
        raise ValueError('Unknown AI task or policy')
    settings = load_settings()
    if guidance is not None:
        if task != 'subscriptions' or not isinstance(guidance, str) or len(guidance) > 12000 or '\x00' in guidance:
            raise ValueError('Subscription guidance must be text of at most 12,000 characters.')
        settings['subscription_guidance'] = guidance.strip()
    if batch_size is not None:
        if task != 'subscriptions' or isinstance(batch_size, bool) or not str(batch_size).isascii() or not str(batch_size).isdigit() or not 1 <= int(batch_size) <= 200:
            raise ValueError('Choose a subscription batch size from 1 to 200.')
        settings['subscriptions_batch_size'] = int(batch_size)
    if task == 'classification':
        if paid_model not in (None, '', 'openrouter-paid') or free_model not in (None, '', 'openrouter-free'):
            raise ValueError('Unknown classification model')
        settings['classify_mode'] = policy
    else:
        registry = ai_model_registry(task)
        current = get_ai_models(task)
        paid_model = current['paid'] if paid_model is None else paid_model
        free_model = current['free'] if free_model is None else free_model
        if paid_model not in registry or registry[paid_model]['model'].endswith(':free'):
            raise ValueError('Choose a paid model or disable this task')
        if free_model not in registry or not registry[free_model]['model'].endswith(':free'):
            raise ValueError('Choose an explicitly free model')
        settings[task + '_ai_policy'] = policy
        settings[task + '_model'] = paid_model
        settings['reply_backup_model' if task == 'reply' else task + '_free_model'] = free_model
    save_settings(settings)


@locked_update
def set_ai_policy(task, policy):
    if task not in AI_TASKS or policy not in MODES:
        raise ValueError('Unknown AI task or policy')
    settings = load_settings()
    settings['classify_mode' if task == 'classification' else task + '_ai_policy'] = policy
    save_settings(settings)


def get_rule_model():
    return load_settings().get("rule_model", DEFAULT_RULE_MODEL)


@locked_update
def set_rule_model(key):
    if key not in RULE_MODELS:
        raise ValueError(f"Unknown rule_model {key!r}, choose from {tuple(RULE_MODELS)}")
    settings = load_settings()
    settings["rule_model"] = key
    if RULE_MODELS[key]['model'].endswith(':free'):
        settings['rule_ai_policy'] = 'free'
    elif key != 'none' and settings.get('rule_ai_policy') == 'free':
        settings['rule_ai_policy'] = 'paid_only'
    save_settings(settings)


def get_reply_model():
    return load_settings().get("reply_model", DEFAULT_REPLY_MODEL)


def free_reply_models():
    # Explicit free model IDs only: a provider's promotional free quota is not a guarantee.
    return {key: backend for key, backend in REPLY_MODELS.items()
            if backend['model'].endswith(':free') and backend['url'] == 'https://openrouter.ai/api/v1/chat/completions'}


def get_reply_backup_model():
    key = load_settings().get('reply_backup_model', 'none')
    return key if key == 'none' or key in free_reply_models() else 'none'


@locked_update
def set_reply_backup_model(key):
    if key != 'none' and key not in free_reply_models():
        raise ValueError('Choose an explicitly free reply model or disable fallback.')
    settings = load_settings()
    settings['reply_backup_model'] = key
    primary = settings.get('reply_model', 'none')
    if settings.get('reply_ai_policy') == 'free' or REPLY_MODELS.get(primary, {}).get('model', '').endswith(':free'):
        settings['reply_ai_policy'] = 'free'
    elif key == 'none':
        settings['reply_ai_policy'] = 'paid_only'
    else:
        settings['reply_ai_policy'] = 'paid'
    save_settings(settings)


@locked_update
def set_reply_model(key):
    if key not in REPLY_MODELS:
        raise ValueError(f"Unknown reply_model {key!r}, choose from {tuple(REPLY_MODELS)}")
    settings = load_settings()
    settings["reply_model"] = key
    if REPLY_MODELS[key]['model'].endswith(':free'):
        settings['reply_ai_policy'] = 'free'
    elif key != 'none' and settings.get('reply_ai_policy') == 'free':
        settings['reply_ai_policy'] = 'paid' if settings.get('reply_backup_model') in free_reply_models() else 'paid_only'
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
    reliably escalates further as the backlog-vs-4-hour gap grows, and backs
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

    paid_count = max(1, min(batch_size, math.ceil(batch_size * paid_fraction)))
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


def get_subscription_batch_size():
    value = load_settings().get("subscriptions_batch_size", 50)
    return value if type(value) is int and 1 <= value <= 200 else 50
