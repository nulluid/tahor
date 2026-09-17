#!/usr/bin/env python3
"""
Read resolved rows out of decisions.db and act on them.

  vendor_mapping -> pure data: write straight into vendor_buckets.json in
    DATA_DIR, commit, push. No review needed -- worst case a message files
    into the wrong folder, easily fixed.

  free_text_rule -> ambiguous: hand the current vendor_buckets.json,
    prompt.txt, and the free text to a model and ask it what kind of
    change this is. A sender-targeted instruction ("block X",
    "unsubscribe me from Y") becomes a real sender_rule, enforced
    immediately by process_batch.py -- no file edit needed. A
    classification-judgment instruction becomes a vendor_buckets.json/
    prompt.txt proposal. All model-generated proposals require explicit review
    before application; sender targets must match an explicit domain. If it says the
    change touches actual script logic, flag it instead -- changing code
    is a deliberate, reviewed step, not something this script does alone.

Run this after the decision app has been used; cron can call it on the
same schedule as the other sweeps.

Requires: whichever of GEMINI_API_KEY / OPENROUTER_API_KEY the current
rule_model setting needs (set from the decision app's settings page --
see mailbox_settings.RULE_MODELS), and DATA_DIR to be a git checkout with
a configured push remote (SSH deploy key or credential helper) if you
want the commit/push step to work.
"""
import json
import hashlib
import difflib
import re
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailbox_settings
import tahor_db
from data_changes import atomic_write, commit_data
import generate_sieve
from http_response import read_bounded, MODEL_RESPONSE_SECONDS

DB_PATH = tahor_db.DB_PATH

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
VENDOR_BUCKETS_PATH = Path(os.environ.get("VENDOR_BUCKETS_PATH", DATA_DIR / "vendor_buckets.json"))
PROMPT_PATH = Path(os.environ.get("PROMPT_PATH", DATA_DIR / "prompt.txt"))

RULE_DRAFTING_SYSTEM_PROMPT = """You maintain a personal email-sweep pipeline. A free-text instruction from
the mailbox's owner can call for one of four kinds of change:

1. sender_rule -- "block/discard/trash all mail (or all marketing mail)
   from X, and/or unsubscribe me from X". This is the right answer for any
   instruction about a specific sender or company, whether or not it also
   mentions unsubscribing. Two granularities: "block_all" (every message
   from that sender is trashed) or "block_marketing" (only messages the
   classifier already tags as marketing are trashed -- receipts, shipping
   notices, and other transactional mail from the same sender still come
   through normally). The owner must explicitly name the full exact domain.
   Never infer a domain from a company name or expand an email address to
   its whole domain. If the exact domain is missing, use needs_clarification.
   If the instruction says or implies "unsubscribe" in addition to blocking,
   set attempt_unsubscribe to true.
2. vendor_buckets.json / prompt.txt edit -- for instructions about how a
   category of mail should be classified or filed in general (not
   targeting one specific sender), or about adding/renaming a filing
   bucket. vendor_buckets.json is a flat map of sender-domain-label ->
   [bucket, display name], pure data. prompt.txt is the classifier's
   system prompt -- be conservative and additive here, prefer one clear
   added rule over rewriting existing ones.
3. needs_code_change -- the instruction genuinely can't be satisfied by
   either of the above (it wants new script behavior, not a data/prompt
   change or a sender rule).
4. needs_clarification -- required scope or details are missing. Ask for the
   missing information and propose no actions or file changes.

You will be given the current contents of both files and the instruction.
Respond with ONLY a JSON object, no markdown fences:
{
  "kind": "sender_rule" | "file_edit" | "needs_code_change" | "needs_clarification",
  "explanation": "one sentence",
  "sender_rule": null or {"domain": "example.com", "rule": "block_all" | "block_marketing", "attempt_unsubscribe": true | false},
  "vendor_buckets_json": null or the FULL new file contents as a JSON string,
  "prompt_txt": null or the FULL new file contents as a string
}
Fill in only the field(s) that match "kind"; leave the rest null. For
needs_code_change or needs_clarification, leave sender_rule/vendor_buckets_json/prompt_txt
all null and explain the missing capability or information.
"""


def _rule_model_call(user_content, selected, system_prompt=None):
    if selected == "none":
        raise ValueError("Rule drafting is disabled; select a private model in Settings")
    backend = mailbox_settings.RULE_MODELS[selected]
    key = os.environ.get(backend["auth_env"])
    if not key:
        raise RuntimeError(f"Set {backend['auth_env']} in the environment for rule_model {backend['model']!r}.")
    payload = {
        "model": backend["model"],
        "messages": [
            {"role": "system", "content": system_prompt if system_prompt is not None else RULE_DRAFTING_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    from model_privacy import private_request_payload
    payload = private_request_payload(backend, payload)
    req = urllib.request.Request(
        backend["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    deadline = time.monotonic() + MODEL_RESPONSE_SECONDS
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(read_bounded(resp, deadline).decode("utf-8"))
    content = body["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    return json.loads(content)


def rule_model_call(user_content, queue_size=1, work_id=None, validate=None, system_prompt=None):
    import ai_routing
    def generate(key):
        proposal = _rule_model_call(user_content, key, system_prompt=system_prompt)
        validate_rule_proposal(proposal)
        if validate is not None:
            try:
                validate(proposal)
            except DomainAuthorizationRequired:
                return {'kind': 'needs_clarification', 'explanation': RULE_CLARIFICATION_QUESTION}
        return proposal
    return ai_routing.run('rule', mailbox_settings.RULE_MODELS,
        generate, queue_size=queue_size, work_id=work_id,
        retryable=(urllib.error.URLError, TimeoutError, OSError, RuntimeError))


def git(*args):
    subprocess.run(["git", "-C", str(DATA_DIR), *args], check=True)


def commit_and_push_data(message):
    paths = [VENDOR_BUCKETS_PATH, PROMPT_PATH, DATA_DIR / "sieve.txt", DATA_DIR / "needs_code_change.md"]
    relative = [str(path.resolve().relative_to(DATA_DIR.resolve())) for path in paths if path.resolve().is_relative_to(DATA_DIR.resolve())]
    return commit_data(DATA_DIR, message, relative)


def apply_vendor_mapping(row, resolution):
    if resolution.get("action") != "map":
        return f"skipped (marked {resolution.get('action')})"
    bucket = resolution.get("bucket", "").strip()
    vendor_name = resolution.get("vendor_name", "").strip()
    if not bucket or not vendor_name:
        return "skipped (missing bucket or vendor name)"

    try:
        context = json.loads(row["context"] or "{}")
    except (ValueError, TypeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    sender_label = context.get("sender_label") or row["summary"].split(":")[-1].strip().split(" ")[0].lower()
    routing_key = context.get('routing_key')
    sender_email = context.get('sender_email')
    if routing_key is not None or sender_email is not None:
        if (not isinstance(routing_key, str) or not isinstance(sender_email, str)
                or routing_key != sender_email or routing_key != routing_key.strip().lower()
                or not re.fullmatch(r'[^\s<>@"\\]+@[a-z0-9.-]+', routing_key)
                or len(routing_key) > 320
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                       for part in routing_key.rsplit('@', 1)[-1].split('.'))):
            raise ValueError('Sender identity changed or is invalid; refresh this routing request')
        sender_label = routing_key
    elif sender_label.lower() == 'shopifyemail' or sender_label.lower() == 'shopifyemail.com' or sender_label.lower().endswith('.shopifyemail.com'):
        raise ValueError('Shared delivery domain needs an exact sender; refresh this routing request')

    buckets = json.loads(VENDOR_BUCKETS_PATH.read_text()) if VENDOR_BUCKETS_PATH.exists() else {}
    buckets[sender_label.lower()] = [bucket, vendor_name]
    atomic_write(VENDOR_BUCKETS_PATH, json.dumps(buckets, indent=2) + "\n")
    return f"mapped {sender_label} -> {bucket}/{vendor_name}"


def apply_sender_rule(sender_rule):
    domain = sender_rule.get("domain", "").strip().lower()
    rule = sender_rule.get("rule")
    if not domain or rule not in tahor_db.SENDER_RULES:
        return f"skipped (bad sender_rule: {sender_rule!r})"

    generate_sieve.domain_test(domain)
    tahor_db.set_sender_rule(domain, rule)
    generate_sieve.refresh_sieve()
    outcome = f"blocked ({rule}) for {domain}"

    if sender_rule.get("attempt_unsubscribe"):
        candidate = tahor_db.get_unsubscribe_candidate(domain)
        if candidate:
            unsub_outcome = tahor_db.execute_unsubscribe(
                candidate,
                os.environ.get("FASTMAIL_EMAIL"),
                os.environ.get("FASTMAIL_APP_PASSWORD"),
            )
            outcome += f"; unsubscribe attempted -- {unsub_outcome}"
        else:
            outcome += "; no tracked unsubscribe link for this sender yet, so unsubscribe wasn't attempted (the block still applies going forward)"
    return outcome


def rule_base_hash(buckets, prompt, instruction):
    return hashlib.sha256(json.dumps([buckets, prompt, instruction]).encode()).hexdigest()


def validate_rule_proposal(result):
    allowed = {'kind', 'explanation', 'sender_rule', 'vendor_buckets_json', 'prompt_txt'}
    if not isinstance(result, dict) or set(result) - allowed:
        raise ValueError('Model returned an invalid rule proposal')
    if 'explanation' in result and not isinstance(result['explanation'], str):
        raise ValueError('Model returned an invalid explanation')
    kind = result.get('kind')
    if kind == 'sender_rule':
        sender = result.get('sender_rule')
        if (not isinstance(sender, dict) or set(sender) != {'domain', 'rule', 'attempt_unsubscribe'}
                or not isinstance(sender['domain'], str)
                or not isinstance(sender['rule'], str)
                or sender['rule'] not in tahor_db.SENDER_RULES
                or type(sender['attempt_unsubscribe']) is not bool
                or result.get('vendor_buckets_json') is not None
                or result.get('prompt_txt') is not None):
            raise ValueError('Sender proposals must contain only an exact domain, supported action, and boolean unsubscribe choice')
    elif kind == 'file_edit':
        if result.get('sender_rule') is not None:
            raise ValueError('File proposals cannot also contain sender actions')
        changes = [result.get('vendor_buckets_json'), result.get('prompt_txt')]
        if all(change is None for change in changes) or any(change is not None and not isinstance(change, str) for change in changes):
            raise ValueError('File proposals must contain text changes')
    elif kind in ('needs_code_change', 'needs_clarification'):
        if kind == 'needs_clarification' and not str(result.get('explanation', '')).strip():
            raise ValueError('Clarification requires an explanation')
        if any(result.get(key) is not None for key in ('sender_rule', 'vendor_buckets_json', 'prompt_txt')):
            raise ValueError('Code-change proposals cannot contain actions to apply')
    else:
        raise ValueError('The model did not return an actionable rule')


RULE_CLARIFICATION_QUESTION = ('Please provide the full exact sender domain for any blocking or unsubscribe rule, '
                               'and clarify any other missing scope in your original instruction. '
                               'A brand name or individual email address does not authorize a domain-wide block.')


class DomainAuthorizationRequired(ValueError):
    pass


class RuleClarificationRequired(ValueError):
    pass


def save_rule_clarification(row, context):
    context.pop('rule_proposal', None)
    context['rule_clarification'] = {'question': RULE_CLARIFICATION_QUESTION}
    database = tahor_db.get_db()
    try:
        with database:
            database.execute("UPDATE decisions SET context=?,status='pending' WHERE id=?",
                             (json.dumps(context), row['id']))
    finally:
        database.close()
    import ai_routing
    ai_routing.record_result('rule', row['id'], True)
    raise RuleClarificationRequired(RULE_CLARIFICATION_QUESTION)


def validate_explicit_sender_target(instruction, sender_rule):
    domain = sender_rule.get('domain', '').strip().lower()
    generate_sieve.domain_test(domain)
    # Email addresses authorize a sender, not every address at its domain.
    without_addresses = re.sub(r'[\w.+-]+@[\w.-]+', '', instruction)
    explicit = {d.lower() for d in re.findall(r'(?<![\w@.-])(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?![\w-]|\.[a-zA-Z0-9])', without_addresses)}
    if domain not in explicit:
        raise DomainAuthorizationRequired('No exact domain authorization: include the full domain explicitly; email-only or brand-only instructions cannot create domain-wide blocks')
    if sender_rule.get('rule') not in tahor_db.SENDER_RULES:
        raise ValueError('Model returned an invalid sender action')


def require_rule_approval(row, resolution, context, result, buckets, prompt, instruction):
    base_hash = context.get('rule_proposal', {}).get('base_hash') or rule_base_hash(buckets, prompt, instruction)
    token = hashlib.sha256(json.dumps([base_hash, result], sort_keys=True).encode()).hexdigest()
    if resolution.get('approved_proposal') == token and context.get('rule_proposal', {}).get('token') == token:
        return
    diffs = []
    for filename, before, after in [('vendor_buckets.json', buckets, result.get('vendor_buckets_json')), ('prompt.txt', prompt, result.get('prompt_txt'))]:
        if after is not None:
            diffs.append(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=filename+' (current)', tofile=filename+' (proposed)')))
    context['rule_proposal'] = {'token': token, 'base_hash': base_hash, 'result': result, 'base_files': {'buckets': buckets, 'prompt': prompt}, 'diff': '\n'.join(diffs)}
    conn = tahor_db.get_db()
    try:
        with conn:
            conn.execute("UPDATE decisions SET context=?, status='pending' WHERE id=?", (json.dumps(context), row['id']))
    finally:
        conn.close()
    raise ValueError('Proposal ready: review the exact changes and approve them on the decisions page')


def pending_ai_rule_ids(database):
    ids = []
    for row in database.execute("SELECT id,context FROM decisions WHERE kind='free_text_rule' AND status IN ('pending','resolved') AND resolution IS NOT NULL"):
        try:
            context = json.loads(row['context'] or '{}')
        except (ValueError, TypeError):
            context = {}
        if not isinstance(context, dict):
            context = {}
        if not context.get('applied') and not context.get('rule_proposal') and not context.get('manual_code_required') and not context.get('rule_clarification'):
            ids.append(row['id'])
    return ids


def apply_free_text_rule(row, resolution):
    text = resolution.get("text", "").strip()
    if not text:
        return "skipped (empty rule text)"

    current_buckets = VENDOR_BUCKETS_PATH.read_text() if VENDOR_BUCKETS_PATH.exists() else "{}"
    current_prompt = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else ""
    user_content = (
        f"Current vendor_buckets.json:\n{current_buckets}\n\n"
        f"Current prompt.txt:\n{current_prompt}\n\n"
        f"Instruction: {text}"
    )
    try:
        context = json.loads(row["context"] or "{}")
    except (ValueError, TypeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    if context.get('rule_clarification'):
        raise RuleClarificationRequired(RULE_CLARIFICATION_QUESTION)
    proposal = context.get('rule_proposal')
    if proposal:
        result = proposal['result']
        validate_rule_proposal(result)
        if proposal.get('base_hash') != rule_base_hash(current_buckets, current_prompt, text):
            original = proposal.get('base_files', {})
            approved = resolution.get('approved_proposal') == proposal.get('token')
            target_buckets = result.get('vendor_buckets_json')
            if target_buckets is not None:
                target_buckets = json.dumps(json.loads(target_buckets), indent=2) + '\n'
            retry_safe = (approved
                and proposal.get('base_hash') == rule_base_hash(original.get('buckets'), original.get('prompt'), text)
                and current_buckets in (original.get('buckets'), target_buckets)
                and current_prompt in (original.get('prompt'), result.get('prompt_txt')))
            if not retry_safe:
                raise ValueError('Rules changed since this preview; reject it and submit a fresh instruction')
    else:
        database = tahor_db.get_db()
        try:
            queued = len(pending_ai_rule_ids(database))
        finally:
            database.close()
        result = rule_model_call(user_content, queue_size=max(1, queued), work_id=row["id"],
            validate=lambda proposal: validate_explicit_sender_target(text, proposal['sender_rule'])
                if proposal.get('kind') == 'sender_rule' else None)
        validate_rule_proposal(result)
    kind = result.get("kind")
    if kind == "needs_clarification":
        save_rule_clarification(row, context)

    if kind == "sender_rule" and result.get("sender_rule"):
        try:
            validate_explicit_sender_target(text, result["sender_rule"])
        except DomainAuthorizationRequired:
            save_rule_clarification(row, context)
        require_rule_approval(row, resolution, context, result, current_buckets, current_prompt, text)
        return apply_sender_rule(result["sender_rule"])

    if kind == "needs_code_change":
        flag_path = DATA_DIR / "needs_code_change.md"
        existing = flag_path.read_text() if flag_path.exists() else "# Rules needing a code change\n\n"
        heading = f"## #{row['id']}:"
        if heading not in existing:
            atomic_write(flag_path, existing + f"{heading} {text}\n\n{result.get('explanation')}\n\n")
        commit_and_push_data("record rule requiring implementation")
        context['manual_code_required'] = True
        database = tahor_db.get_db()
        try:
            with database:
                database.execute("UPDATE decisions SET context=?,status='pending' WHERE id=?", (json.dumps(context), row['id']))
        finally:
            database.close()
        raise ValueError(f"This rule requires a code change and has not been applied: {result.get('explanation')}")

    if kind != "file_edit":
        raise ValueError("The model did not return an actionable rule")
    changed = []
    new_buckets = result.get("vendor_buckets_json")
    new_prompt = result.get("prompt_txt")
    if new_buckets is not None:
        parsed_buckets = json.loads(new_buckets)
        if not isinstance(parsed_buckets, dict) or any(not isinstance(value, list) or len(value) != 2 or not all(isinstance(part, str) and part.strip() and not any(c in part for c in '\r\n"\\') for part in value) for value in parsed_buckets.values()):
            raise ValueError("Model returned invalid vendor mappings")
    if new_prompt is not None and (not isinstance(new_prompt, str) or not new_prompt.strip()):
        raise ValueError("Model returned an invalid prompt")
    require_rule_approval(row, resolution, context, result, current_buckets, current_prompt, text)
    if new_buckets is not None:
        atomic_write(VENDOR_BUCKETS_PATH, json.dumps(parsed_buckets, indent=2) + "\n")
        changed.append("vendor_buckets.json")
    if new_prompt is not None:
        atomic_write(PROMPT_PATH, new_prompt)
        changed.append("prompt.txt")

    return f"applied: {', '.join(changed) or 'no file changes'} — {result.get('explanation')}"


def apply_one(decision_id):
    import fcntl
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / ".decisions.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        conn = tahor_db.get_db()
        try:
            row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if row is None or not row["resolution"]:
                raise ValueError("Decision is missing its resolution")
            try:
                context = json.loads(row["context"] or "{}")
            except (ValueError, TypeError):
                context = {"note": row["context"]}
            if not isinstance(context, dict):
                context = {"note": row["context"]}
            if context.get("applied"):
                return context.get("outcome", "Already applied")
            resolution = json.loads(row["resolution"])
            if row["kind"] == "vendor_mapping":
                outcome = apply_vendor_mapping(row, resolution)
            elif row["kind"] == "free_text_rule":
                try:
                    outcome = apply_free_text_rule(row, resolution)
                except RuleClarificationRequired:
                    return "Waiting for rule clarification"
            elif row["kind"] == "message_review":
                import keyword_tool
                action = resolution.get("action")
                if action not in ("keep", "trash"):
                    raise ValueError("Choose Keep or Trash for this message")
                add = ["retention-standard"] if action == "keep" else ["retention-transient", "category-marketing", "delete-pending"]
                result = keyword_tool.apply_ops([{"mailbox": context["mailbox"], "message_id": context["message_id"], "uid": context.get("uid"), "uidvalidity": context.get("uidvalidity"), "add": add, "delete": action == "trash", "remove": ["retention-pending-review", "needs-attention"] + (["delete-pending"] if action == "keep" else [])}])
                if context["message_id"] not in result["applied"]:
                    raise RuntimeError("Message operation could not be completed; it remains available for retry")
                outcome = "Message kept" if action == "keep" else "Message deleted"
            else:
                raise ValueError("This decision needs manual review; no mailbox action was applied")
            commit_and_push_data("apply mailbox decision")
            context.update(applied=True, outcome=outcome)
            with conn:
                conn.execute("UPDATE decisions SET context=? WHERE id=?", (json.dumps(context), decision_id))
            return outcome
        finally:
            conn.close()


def main():
    import ai_routing
    conn = tahor_db.get_db()
    try:
        pending_ids = pending_ai_rule_ids(conn)
        resolved_ids = []
        for row in conn.execute("SELECT id,context FROM decisions WHERE status='resolved' AND resolution IS NOT NULL"):
            try:
                context = json.loads(row['context'] or '{}')
            except (ValueError, TypeError):
                context = {}
            if isinstance(context, dict) and context.get('rule_clarification'):
                continue
            resolved_ids.append(row['id'])
        ids = list(dict.fromkeys(resolved_ids + pending_ids))
        ai_routing.reconcile_pending('rule', pending_ids)
    finally:
        conn.close()
    failures = 0
    for decision_id in ids:
        try:
            apply_one(decision_id)
            print(f"{decision_id}: decision processed")
        except Exception as exc:
            failures += 1
            print(f"{decision_id}: could not apply ({type(exc).__name__}); work remains pending", file=sys.stderr)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
