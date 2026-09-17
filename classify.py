#!/usr/bin/env python3
"""Classify email through a local model or privacy-constrained OpenRouter route.

CLASSIFY_BACKEND selects local, openrouter, openrouter-free, or openrouter-paid
for this standalone utility. The legacy direct Gemini route is disabled until
its account-specific privacy terms can be verified. Hosted inference always
requires zero-data-retention endpoints with provider data collection denied.
A compliant endpoint must be available; privacy filters are never relaxed.

TAHOR_CLASSIFY_FREE_ENABLED=0 blocks free requests, including paid fallback.
The continuous worker selects its tiers through Settings, independently of
CLASSIFY_BACKEND. Provider availability and rate limits can affect every tier.
Local inference needs a running OpenAI-compatible server and local resources.

Usage:
  python3 classify.py <input.json> <output.json> <system_prompt.txt> [--concurrency N] [--model NAME]

Inputs contain id, subject, from, date and snippet, plus optional hint_* fields.
The classifier validates actions and core fields before returning a decision;
invalid responses remain errors for retry rather than mailbox instructions.
"""
import json
from http_response import read_bounded, MODEL_RESPONSE_SECONDS
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def _gemini_key():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("Set GEMINI_API_KEY in your environment for CLASSIFY_BACKEND=gemini.")
    return key


def _openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("Set OPENROUTER_API_KEY in your environment for CLASSIFY_BACKEND=openrouter.")
    return key


def paid_concurrency():
    value = int(os.environ.get("TAHOR_PAID_CONCURRENCY", "8"))
    if not 1 <= value <= 64:
        raise ValueError("TAHOR_PAID_CONCURRENCY must be between 1 and 64")
    return value


_paid_pacing_lock = threading.Lock()
_paid_next_start = {}


def paid_request_interval():
    value = float(os.environ.get("TAHOR_PAID_REQUEST_INTERVAL_SECONDS", "3"))
    if not 1 <= value <= 60:
        raise ValueError("TAHOR_PAID_REQUEST_INTERVAL_SECONDS must be between 1 and 60")
    return value


def wait_for_paid_request(url, model, provider):
    if (url != 'https://openrouter.ai/api/v1/chat/completions'
            or model != 'google/gemini-3.8-flash'):
        return
    interval = paid_request_interval()
    route = (url, model, tuple(provider.get('only', [])))
    # Serialize start reservations, not network requests. Holding this lock
    # through the wait prevents delayed callers from releasing a queued burst.
    with _paid_pacing_lock:
        delay = _paid_next_start.get(route, 0) - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _paid_next_start[route] = time.monotonic() + interval


# Hosted and local models share the OpenAI-compatible request shape.
BACKENDS = {
    "local": {
        "url": "http://localhost:1234/v1/chat/completions",
        "default_model": "qwen3-30b-a3b-instruct-2507",
        "default_concurrency": 6,
        "auth_header": None,
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "default_model": "gemini-3.5-flash-lite",
        "default_concurrency": 2,  # this box is 1 vCPU; concurrency 4 reliably hung mid-batch, 2 is proven stable
        "auth_header": lambda: f"Bearer {_gemini_key()}",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        # Same model/calibration as the "local" backend, just hosted -- no
        # prompt revalidation needed. Paid but genuinely cheap (~$0.05-0.20
        # per 1000 emails at this snippet size).
        "default_model": "qwen/qwen3-30b-a3b-instruct-2507",
        "default_concurrency": 2,
        "auth_header": lambda: f"Bearer {_openrouter_key()}",
    },
    "openrouter-free": {
        # Privacy-restricted experimental route. Disabled by default because
        # the current free candidates did not pass classification evaluation.
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "inclusionai/ling-3.0-flash-vl:free",
        "default_concurrency": 2,
        "auth_header": lambda: f"Bearer {_openrouter_key()}",
        "request_options": {
            "reasoning": {"enabled": False},
            "provider": {"only": ["novita"], "allow_fallbacks": False,
                         "max_price": {"prompt": 0, "completion": 0}},
        },
    },
    "openrouter-paid": {
        # Evaluated on real and synthetic messages; provider capacity must be
        # measured separately from the previous model's concurrency trials.
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "google/gemini-3.8-flash",
        "default_concurrency": paid_concurrency(),
        "auth_header": lambda: f"Bearer {_openrouter_key()}",
        "request_options": {
            "reasoning": {"effort": "low"},
            "max_tokens": 2048,
            "provider": {"only": ["google-vertex/global"], "allow_fallbacks": False},
        },
    },
}
SCHEMA_FIELDS = ["category", "retention", "expense_type", "needs_attention", "folder_domain"]


def free_classification_enabled():
    return os.environ.get("TAHOR_CLASSIFY_FREE_ENABLED", "0").strip() == "1"


def free_disabled_results(records):
    return [{"id": record["id"], "action": "error",
             "reason": "Free classification is disabled; message retained for retry"}
            for record in records]


def classify_one(url, headers, model, system_prompt, record, retries=3):
    if url == BACKENDS["gemini"]["url"]:
        return {"id": record["id"], "action": "error",
                "reason": "Direct Google classification is disabled until account privacy is verified"}
    if (url == BACKENDS["openrouter-free"]["url"] and model.endswith(":free")
            and not free_classification_enabled()):
        return free_disabled_results([record])[0]
    hints = [f"{k[5:]}={v}" if not isinstance(v, bool) else k[5:]
             for k, v in record.items() if k.startswith("hint_") and v]
    hint_line = f"Hints (context only, not decisive): {', '.join(hints)}\n" if hints else ""

    user_content = (
        f"Subject: {record.get('subject', '')}\n"
        f"From: {record.get('from', '')}\n"
        f"Date: {record.get('date', '')}\n"
        f"{hint_line}"
        f"Body/snippet: {record.get('snippet', '')}"
    )
    import reply_rules
    rules = reply_rules.get_rules()
    system_prompt = reply_rules.classification_prompt(system_prompt, rules)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 1024,
    }
    for backend in BACKENDS.values():
        if backend['url'] == url and backend['default_model'] == model:
            payload.update(backend.get('request_options', {}))
            break
    if url == BACKENDS["openrouter-paid"]["url"]:
        # Enforce endpoint policy on every hosted request, including retries;
        # never rely on a provider's current catalog membership alone.
        provider = dict(payload.get('provider', {}))
        provider.update(zdr=True, data_collection='deny')
        payload['provider'] = provider
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            wait_for_paid_request(url, model, payload.get('provider', {}))
            deadline = time.monotonic() + MODEL_RESPONSE_SECONDS
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(read_bounded(resp, deadline).decode("utf-8"))
            content = body["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            parsed = json.loads(content)
            if not isinstance(parsed, dict) or parsed.get("action") not in ("keep", "trash", "mixed"):
                raise ValueError("Invalid classification action")
            if parsed.get("action") != "trash":
                if parsed.get("retention") not in ("transient", "standard", "forever", "pending-review"):
                    raise ValueError("Invalid retention tier")
                if not isinstance(parsed.get("category"), str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", parsed["category"]):
                    raise ValueError("Invalid category")
                if parsed.get("expense_type") not in ("business", "personal", "n/a", "", None):
                    raise ValueError("Invalid expense type")
                if not isinstance(parsed.get("needs_attention"), bool):
                    raise ValueError("Invalid attention flag")
            result = {"id": record["id"], "action": parsed["action"], "reason": str(parsed.get("reason", ""))}
            result.update({field: parsed.get(field, "") for field in SCHEMA_FIELDS})
            if result["action"] == "trash":
                result.update(category="marketing", retention="transient", expense_type="n/a", needs_attention=False, folder_domain="Other")
            matches, uncertain = reply_rules.classification_matches(parsed, rules, record.get('from', ''))
            if rules:
                result['reply_rule_matches'], result['reply_rule_uncertain'] = matches, uncertain
                result['reply_rule_versions'] = {r['id']: r.get('revision', r['id']) for r in rules}
            return result
        except urllib.error.HTTPError as e:
            last_err = e
            try:
                e.close()
            except Exception:
                pass  # Cleanup must not interrupt provider recovery.
            if e.code in (400, 401, 402, 403, 404):
                break  # These need configuration, credit, or a different backend.
            if e.code == 429 and attempt < retries:  # rate limited — back off and retry
                time.sleep(2 ** attempt * 2)
                continue
            continue
        except Exception as e:
            last_err = e
            continue
    result = {"id": record["id"], "action": "error", "reason": f"classification failed: {last_err}"}
    if isinstance(last_err, urllib.error.HTTPError):
        result["http_status"] = last_err.code
    return result


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    input_path, output_path, prompt_path = sys.argv[1:4]

    backend_name = os.environ.get("CLASSIFY_BACKEND", "local")
    if backend_name not in BACKENDS:
        raise SystemExit(f"Unknown CLASSIFY_BACKEND={backend_name!r}. Choose from: {', '.join(BACKENDS)}")
    backend = BACKENDS[backend_name]

    if backend_name == "openrouter-free" and not free_classification_enabled():
        with open(input_path) as f:
            records = json.load(f)
        with open(output_path, "w") as f:
            json.dump(free_disabled_results(records), f, indent=1)
        return

    concurrency, model = backend["default_concurrency"], backend["default_model"]
    for i, arg in enumerate(sys.argv):
        if arg == "--concurrency" and i + 1 < len(sys.argv):
            concurrency = int(sys.argv[i + 1])
        if arg == "--model" and i + 1 < len(sys.argv):
            model = sys.argv[i + 1]

    headers = {"Content-Type": "application/json"}
    if backend["auth_header"]:
        headers["Authorization"] = backend["auth_header"]()

    with open(input_path) as f:
        records = json.load(f)
    with open(prompt_path) as f:
        system_prompt = f.read()

    print(f"Backend: {backend_name} ({model})", file=sys.stderr)

    results = [None] * len(records)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(classify_one, backend["url"], headers, model, system_prompt, rec): i
                   for i, rec in enumerate(records)}
        done = 0
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
            done += 1
            if done % 25 == 0 or done == len(records):
                print(f"  {done}/{len(records)} classified", file=sys.stderr)

    with open(output_path, "w") as f:
        json.dump(results, f, indent=1)

    counts = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    print(f"Done. {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
