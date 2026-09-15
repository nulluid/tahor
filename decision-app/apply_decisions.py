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
    prompt.txt edit, applied and pushed the same as above. If it says the
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
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailbox_settings
import tahor_db

DB_PATH = tahor_db.DB_PATH

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
VENDOR_BUCKETS_PATH = DATA_DIR / "vendor_buckets.json"
PROMPT_PATH = DATA_DIR / "prompt.txt"

RULE_DRAFTING_SYSTEM_PROMPT = """You maintain a personal email-sweep pipeline. A free-text instruction from
the mailbox's owner can call for one of three kinds of change:

1. sender_rule -- "block/discard/trash all mail (or all marketing mail)
   from X, and/or unsubscribe me from X". This is the right answer for any
   instruction about a specific sender or company, whether or not it also
   mentions unsubscribing. Two granularities: "block_all" (every message
   from that sender is trashed) or "block_marketing" (only messages the
   classifier already tags as marketing are trashed -- receipts, shipping
   notices, and other transactional mail from the same sender still come
   through normally). Guess the sender's real email domain from the
   company/brand name in the instruction (e.g. "Kate Spade" ->
   katespade.com) -- use your own knowledge of the company, don't guess a
   generic pattern. If the instruction says or implies "unsubscribe" in
   addition to blocking, set attempt_unsubscribe to true.
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

You will be given the current contents of both files and the instruction.
Respond with ONLY a JSON object, no markdown fences:
{
  "kind": "sender_rule" | "file_edit" | "needs_code_change",
  "explanation": "one sentence",
  "sender_rule": null or {"domain": "example.com", "rule": "block_all" | "block_marketing", "attempt_unsubscribe": true | false},
  "vendor_buckets_json": null or the FULL new file contents as a JSON string,
  "prompt_txt": null or the FULL new file contents as a string
}
Fill in only the field(s) that match "kind"; leave the rest null. For
needs_code_change, leave sender_rule/vendor_buckets_json/prompt_txt all
null and explain what script behavior would need to change.
"""


def rule_model_call(user_content):
    backend = mailbox_settings.RULE_MODELS[mailbox_settings.get_rule_model()]
    key = os.environ.get(backend["auth_env"])
    if not key:
        raise SystemExit(f"Set {backend['auth_env']} in the environment for rule_model {backend['model']!r}.")
    payload = {
        "model": backend["model"],
        "messages": [
            {"role": "system", "content": RULE_DRAFTING_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        backend["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    return json.loads(content)


def git(*args):
    subprocess.run(["git", "-C", str(DATA_DIR), *args], check=True)


def commit_and_push_data(message):
    git("add", "-A")
    result = subprocess.run(
        ["git", "-C", str(DATA_DIR), "diff", "--cached", "--quiet"]
    )
    if result.returncode == 0:
        return False  # nothing changed
    git("commit", "-m", message)
    git("push", "origin", "main")
    return True


def apply_vendor_mapping(row, resolution):
    if resolution.get("action") != "map":
        return f"skipped (marked {resolution.get('action')})"
    bucket = resolution.get("bucket", "").strip()
    vendor_name = resolution.get("vendor_name", "").strip()
    if not bucket or not vendor_name:
        return "skipped (missing bucket or vendor name)"

    context = json.loads(row["context"] or "{}") if isinstance(row["context"], str) else {}
    sender_label = context.get("sender_label") or row["summary"].split(":")[-1].strip().split(" ")[0].lower()

    buckets = json.loads(VENDOR_BUCKETS_PATH.read_text()) if VENDOR_BUCKETS_PATH.exists() else {}
    buckets[sender_label.lower()] = [bucket, vendor_name]
    VENDOR_BUCKETS_PATH.write_text(json.dumps(buckets, indent=2) + "\n")
    return f"mapped {sender_label} -> {bucket}/{vendor_name}"


def apply_sender_rule(sender_rule):
    domain = sender_rule.get("domain", "").strip().lower()
    rule = sender_rule.get("rule")
    if not domain or rule not in tahor_db.SENDER_RULES:
        return f"skipped (bad sender_rule: {sender_rule!r})"

    tahor_db.set_sender_rule(domain, rule)
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
    result = rule_model_call(user_content)
    kind = result.get("kind")

    if kind == "sender_rule" and result.get("sender_rule"):
        return apply_sender_rule(result["sender_rule"])

    if kind == "needs_code_change" or result.get("needs_code_change"):
        flag_path = DATA_DIR / "decision-app" / "needs_code_change.md"
        existing = flag_path.read_text() if flag_path.exists() else "# Rules needing a code change\n\n"
        flag_path.write_text(
            existing + f"## #{row['id']}: {text}\n\n{result.get('explanation')}\n\n"
        )
        return f"flagged for manual review (needs code change): {result.get('explanation')}"

    changed = []
    if result.get("vendor_buckets_json"):
        VENDOR_BUCKETS_PATH.write_text(result["vendor_buckets_json"])
        changed.append("vendor_buckets.json")
    if result.get("prompt_txt"):
        PROMPT_PATH.write_text(result["prompt_txt"])
        changed.append("prompt.txt")
    return f"applied: {', '.join(changed) or 'no file changes'} — {result.get('explanation')}"


def main():
    conn = tahor_db.get_db()
    rows = conn.execute(
        "SELECT * FROM decisions WHERE status = 'resolved' AND resolution IS NOT NULL "
        "AND (context IS NULL OR context NOT LIKE '%\"applied\": true%')"
    ).fetchall()

    if not rows:
        print("Nothing to apply.")
        return

    results = []
    for row in rows:
        resolution = json.loads(row["resolution"])
        if row["kind"] == "vendor_mapping":
            outcome = apply_vendor_mapping(row, resolution)
        elif row["kind"] == "free_text_rule":
            outcome = apply_free_text_rule(row, resolution)
        else:
            outcome = "skipped (unknown kind)"
        results.append((row["id"], outcome))
        try:
            context = json.loads(row["context"]) if row["context"] else {}
            if not isinstance(context, dict):
                context = {"note": row["context"]}
        except (json.JSONDecodeError, TypeError):
            context = {"note": row["context"]}
        context["applied"] = True
        context["outcome"] = outcome
        conn.execute("UPDATE decisions SET context = ? WHERE id = ?", (json.dumps(context), row["id"]))
        conn.commit()

    if commit_and_push_data("apply resolved decisions"):
        print("Pushed data changes.")

    for decision_id, outcome in results:
        print(f"#{decision_id}: {outcome}")


if __name__ == "__main__":
    main()
