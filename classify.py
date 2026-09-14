#!/usr/bin/env python3
"""
Classify a batch of emails through a local LLM (LM Studio's OpenAI-compatible
API by default) instead of spending tokens on a hosted model.

Usage:
  python3 classify.py <input.json> <output.json> <system_prompt.txt> [--concurrency N] [--model NAME]

Input record: {"id": "...", "subject": "...", "from": "...", "date": "...", "snippet": "..."}
plus any optional hint_* fields your prompt wants surfaced as weak priors.

Output fields beyond id/action/reason are passed through verbatim from
whatever the model returns — the system prompt owns that schema, not this
script. See prompt.example.txt for the schema this project was built around.
"""
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

LM_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen3-30b-a3b-instruct-2507"
SCHEMA_FIELDS = ["category", "retention", "expense_type", "needs_attention", "folder_domain"]


def classify_one(model, system_prompt, record, retries=2):
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
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 200,
    }
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(LM_URL, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            parsed = json.loads(content)
            result = {"id": record["id"], "action": parsed.get("action", "unsure"), "reason": parsed.get("reason", "")}
            result.update({field: parsed.get(field, "") for field in SCHEMA_FIELDS})
            return result
        except Exception as e:
            last_err = e
            continue
    return {"id": record["id"], "action": "error", "reason": f"classification failed: {last_err}"}


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    input_path, output_path, prompt_path = sys.argv[1:4]
    concurrency, model = 6, DEFAULT_MODEL
    for i, arg in enumerate(sys.argv):
        if arg == "--concurrency" and i + 1 < len(sys.argv):
            concurrency = int(sys.argv[i + 1])
        if arg == "--model" and i + 1 < len(sys.argv):
            model = sys.argv[i + 1]

    with open(input_path) as f:
        records = json.load(f)
    with open(prompt_path) as f:
        system_prompt = f.read()

    results = [None] * len(records)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(classify_one, model, system_prompt, rec): i for i, rec in enumerate(records)}
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
