"""Durable task-level AI routing. State contains timing and model hashes, never content."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import time
import uuid

from data_changes import atomic_write
import mailbox_settings
import tahor_db

COOLDOWN_SECONDS = 300
QUEUE_LIMIT_SECONDS = 4 * 3600
ALERT_SECONDS = 30 * 60
DEFAULT_FREE_SECONDS = 60
TASKS = ('classification', 'reply', 'rule')


class RoutingUnavailable(ValueError):
    """Work remains queued until its permitted provider can be retried."""


def state_path():
    return tahor_db.DB_PATH.parent / 'ai_routing_state.json'


@contextmanager
def locked_state():
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
            if not isinstance(state, dict):
                raise ValueError('Invalid AI routing state')
        except FileNotFoundError:
            state = {}
        except (ValueError, UnicodeError):
            # Retain private evidence, then recover timing state; model policy remains
            # authoritative in settings, so a reset cannot enable a forbidden tier.
            quarantine = path.with_name(path.name + '.corrupt-' + uuid.uuid4().hex)
            path.chmod(0o600)
            path.replace(quarantine)
            state = {}
        for task in TASKS:
            entry = state.get(task)
            if not isinstance(entry, dict):
                state.pop(task, None)
                continue
            if not isinstance(entry.get('tiers'), dict):
                entry['tiers'] = {}
            entry['tiers'] = {key: value for key, value in entry['tiers'].items() if isinstance(value, dict)}
            failures = entry.get('failures', {})
            entry['failures'] = {key: value for key, value in failures.items()
                                 if isinstance(key, str) and type(value) in (int, float) and math.isfinite(value)} if isinstance(failures, dict) else {}
        yield state
        atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
        path.chmod(0o600)


def _number(value, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) else default


def _identity(policy, models, registry):
    return hashlib.sha256(json.dumps([policy, models, {k: registry.get(v) for k, v in models.items()}], sort_keys=True).encode()).hexdigest()


def _entry(state, task, identity):
    value = state.get(task)
    if not isinstance(value, dict) or value.get('identity') != identity:
        value = {'identity': identity, 'tiers': {}}
        state[task] = value
    return value


def _cooling(tier, now):
    until = _number(tier.get('retry_at'))
    return now < until <= now + COOLDOWN_SECONDS


def _work_key(work_id):
    return hashlib.sha256(str(work_id if work_id is not None else 'default').encode()).hexdigest()


def _failure(entry, now, work):
    failures = entry.setdefault('failures', {})
    failures.setdefault(work, now)
    entry['failure_since'] = min(failures.values())
    entry['last_failure'] = now


def record_results(task, results):
    """Record one completed classifier batch with a single atomic state write."""
    if task not in TASKS:
        raise ValueError('Unknown AI task')
    with locked_state() as state:
        entry = state.setdefault(task, {'tiers': {}})
        failures = entry.setdefault('failures', {})
        for result in results:
            work = _work_key(result['id'])
            if result.get('action') == 'error':
                _failure(entry, time.time(), work)
            else:
                failures.pop(work, None)
        if failures:
            entry['failure_since'] = min(failures.values())
        else:
            entry.pop('failure_since', None)
            entry.pop('last_failure', None)


def record_result(task, work_id, success):
    record_results(task, [{'id': work_id, 'action': 'ok' if success else 'error'}])


def reconcile_pending(task, work_ids):
    """Drop failure markers for work withdrawn/completed outside the model call."""
    active = {_work_key(key) for key in work_ids}
    with locked_state() as state:
        entry = state.get(task)
        if not isinstance(entry, dict):
            return
        failures = {key: value for key, value in entry.get('failures', {}).items() if key in active}
        entry['failures'] = failures
        if failures:
            entry['failure_since'] = min(failures.values())
        else:
            entry.pop('failure_since', None)
            entry.pop('last_failure', None)


def run(task, registry, operation, queue_size=1, retryable=(OSError, TimeoutError), work_id=None):
    """Call operation(model_key) under the task policy; quality failures never buy a retry.

    Queue ETA uses an exponentially smoothed observed free-operation duration,
    or a conservative 60-second estimate before a successful free observation.
    Failures cool only their tier for five minutes. The caller owns durable work.
    """
    if task not in TASKS:
        raise ValueError('Unknown AI task')
    if not mailbox_settings.is_ai_enabled(task):
        raise RoutingUnavailable('AI task is disabled; work remains pending')
    policy = mailbox_settings.get_ai_policy(task)
    if policy not in ('paid_only', 'paid', 'auto', 'free'):
        raise RoutingUnavailable('AI routing policy needs configuration')
    models = mailbox_settings.get_ai_models(task)
    identity = _identity(policy, models, registry)
    work = _work_key(work_id)
    now = time.time()
    with locked_state() as state:
        entry = _entry(state, task, identity)
        latency = max(0.001, _number(entry.get('free_seconds'), DEFAULT_FREE_SECONDS))
        eta = max(1, _number(queue_size, 1)) * latency
    if policy == 'paid_only':
        order = ['paid']
    elif policy == 'free':
        order = ['free']
    elif policy == 'paid':
        order = ['paid', 'free']
    else:
        order = ['paid', 'free'] if eta > QUEUE_LIMIT_SECONDS else ['free', 'paid']
    last_error = None
    for tier in order:
        if (not mailbox_settings.is_ai_enabled(task)
                or mailbox_settings.get_ai_policy(task) != policy
                or mailbox_settings.get_ai_models(task) != models):
            raise RoutingUnavailable('AI settings changed; retry queued work under the new policy')
        key = models.get(tier)
        backend = registry.get(key)
        # Never infer that an unknown model is free, or call a paid model in a free slot.
        if not backend or key == 'none' or (bool(backend.get('free', False)) or str(backend.get('model', backend.get('default_model', ''))).endswith(':free')) != (tier == 'free'):
            continue
        now = time.time()
        with locked_state() as state:
            entry = _entry(state, task, identity)
            cooling = _cooling(entry['tiers'].get(tier, {}), now)
        if cooling:
            continue
        started = time.monotonic()
        try:
            result = operation(key)
        except retryable as error:
            last_error = error
            with locked_state() as state:
                entry = _entry(state, task, identity)
                entry['tiers'][tier] = {'retry_at': time.time() + COOLDOWN_SECONDS}
            # Do not persist provider messages, response bodies or credentials.
            close = getattr(error, 'close', None)
            if close:
                try:
                    close()
                except Exception:
                    pass
            continue
        except Exception:
            with locked_state() as state:
                _failure(_entry(state, task, identity), time.time(), work)
            raise
        else:
            elapsed = max(0.001, time.monotonic() - started)
            with locked_state() as state:
                entry = _entry(state, task, identity)
                entry['tiers'].pop(tier, None)
                failures = entry.setdefault('failures', {})
                failures.pop(work, None)
                if failures:
                    entry['failure_since'] = min(failures.values())
                else:
                    entry.pop('failure_since', None)
                    entry.pop('last_failure', None)
                if tier == 'free':
                    old = _number(entry.get('free_seconds'), elapsed)
                    entry['free_seconds'] = 0.75 * old + 0.25 * elapsed
            return result
    with locked_state() as state:
        _failure(_entry(state, task, identity), time.time(), work)
    if last_error is not None:
        raise last_error
    raise RoutingUnavailable('Permitted AI providers are cooling down or unavailable; work remains pending')


def persistent_problems(now=None):
    now = time.time() if now is None else now
    with locked_state() as state:
        problems = []
        for task in TASKS:
            if not mailbox_settings.is_ai_enabled(task):
                continue
            entry = state.get(task, {})
            since = entry.get('failure_since') if isinstance(entry, dict) else None
            if type(since) in (int, float) and math.isfinite(since) and now - since >= ALERT_SECONDS:
                problems.append('ai_' + task)
        return problems
