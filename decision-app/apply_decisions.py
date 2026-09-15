#!/usr/bin/env python3
"""
Read resolved rows out of decisions.db and act on them.

  vendor_mapping -> pure data: write straight into vendor_buckets.json in
    DATA_DIR, commit, push. No review needed -- worst case a message files
    into the wrong folder, easily fixed.

  free_text_rule -> ambiguous: hand the current vendor_buckets.json,
    prompt.txt, and the free text to an LLM and ask it to propose a change.
    If it says the change is data/prompt-only, apply and push the same as
    above. If it says the change touches actual script logic, flag it
    instead of touching the scripts -- changing code is a deliberate,
    reviewed step, not something this script does on its own.

Run this after the decision app has been used; cron can call it on the
same schedule as the other sweeps.

Requires: GEMINI_API_KEY in the environment for the rule-drafting step
(separate from whatever CLASSIFY_BACKEND you picked for classify.py --
this always uses Gemini's OpenAI-compatible endpoint unless you change
RULE_DRAFTING_MODEL and the request URL below to match another provider),
and DATA_DIR to be a git checkout with a configured push remote (SSH
deploy key or credential helper) if you want the commit/push step to work.
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).parent / "decisions.db"

# DATA_DIR holds vendor_buckets.json and prompt.txt -- the same gitignored
# config files classify.py/config.py read from the repo root. Defaults to
# this repo (one directory up from decision-app/), but can point at a
# separate private git repo if you want that data to have its own tracked
# history independent of this codebase.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
VENDOR_BUCKETS_PATH = DATA_DIR / "vendor_buckets.json"
PROMPT_PATH = DATA_DIR / "prompt.txt"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
RULE_DRAFTING_MODEL = os.environ.get("RULE_DRAFTING_MODEL", "gemini-3.6-flash")

GEMINI_SYSTEM_PROMPT = """You maintain two files for a personal email-sweep pipeline:

1. vendor_buckets.json -- a flat map of sender-domain-label -> [bucket, display name].
   Pure data, no code. Safe to change freely.
2. prompt.txt -- the system prompt a classifier model reads to decide
   keep/trash/retention-tier for each email. Also just text, but changes
   here affect judgment broadly, so be conservative and additive: prefer
   adding one clear rule over rewriting existing rules.

You will be given the current contents of both files and a free-text
instruction from the mailbox's owner. Decide what changes accomplish the
instruction using ONLY these two files -- never propose changing any
Python script's logic. If the instruction genuinely cannot be satisfied
by editing these two data/text files alone, say so.

Respond with ONLY a JSON object, no markdown fences:
{
  "needs_code_change": false,
  "explanation": "one sentence",
  "vendor_buckets_json": null or the FULL new file contents as a JSON string,
  "prompt_txt": null or the FULL new file contents as a string
}
Only include a new value for a file you're actually changing; leave the
other null. If needs_code_change is true, leave both null and explain
what script behavior would need to change and why these two files aren't
enough.
"""


def gemini_call(user_content):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("Set GEMINI_API_KEY in the environment.")
    payload = {
        "model": RULE_DRAFTING_MODEL,
        "messages": [
            {"role": "system", "content": GEMINI_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        GEMINI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
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
    result = gemini_call(user_content)

    if result.get("needs_code_change"):
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
    return f"applied via LLM: {', '.join(changed) or 'no file changes'} — {result.get('explanation')}"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        conn.execute(
            "UPDATE decisions SET context = json_set(COALESCE(context, '{}'), '$.applied', true, '$.outcome', ?) WHERE id = ?",
            (outcome, row["id"]),
        )
        conn.commit()

    if commit_and_push_data("apply resolved decisions"):
        print("Pushed data changes.")

    for decision_id, outcome in results:
        print(f"#{decision_id}: {outcome}")


if __name__ == "__main__":
    main()
