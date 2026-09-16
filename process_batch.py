#!/usr/bin/env python3
"""
Turn one batch's classification output into the two files the rest of the
pipeline needs: a trash list and a keyword_tool.py ops file. Also applies a
"keep one message per sender" rule so a sender that's 100% trash this batch
still leaves one dated record behind, tagged retention-forever.

Also enforces standing sender rules from the unsubscribe page and records unsubscribe candidates.

Usage: python3 process_batch.py <prefix> <mailbox_imap_path>
Requires in cwd: <prefix>_in.json, <prefix>_out.json, <prefix>_env.json
(the last one from bulk_lookup.py, used to resolve each id's Message-ID)
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tahor_db


def sender_domain_of(email_addr):
    return (email_addr or "").rsplit("@", 1)[-1].lower() if "@" in (email_addr or "") else None

_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def norm(s):
    return re.sub(r"\s+", " ", (s or "").translate(_QUOTE_MAP)).strip()


def parse_jmap(dt):
    parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    # A Date: header missing a timezone parses naive; parse_internaldate()
    # is always aware (IMAP INTERNALDATE always carries an offset) -- treat
    # a timezone-less header as UTC so the two are always comparable.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_internaldate(s):
    return datetime.strptime(s.strip(), "%d-%b-%Y %H:%M:%S %z")


def main():
    prefix, mailbox = sys.argv[1], sys.argv[2]
    inrecs = {r["id"]: r for r in json.loads(Path(f"{prefix}_in.json").read_text())}
    outrecs = json.loads(Path(f"{prefix}_out.json").read_text())
    envs = json.loads(Path(f"{prefix}_env.json").read_text())

    for r in outrecs:
        rec = inrecs.get(r["id"])
        if not rec:
            continue
        domain = sender_domain_of(rec["from"])
        rule = tahor_db.get_sender_rule(domain)
        if rule == "block_all" or (rule == "block_marketing" and r.get("category") == "marketing"):
            r["action"] = "trash"
            r["reason"] = f"sender rule: {rule}"

    for e in envs:
        if not (e.get("unsubscribe_url") or e.get("unsubscribe_mailto")):
            continue
        domain = sender_domain_of(e["from_email"])
        if domain and tahor_db.get_sender_rule(domain) is None:
            tahor_db.upsert_unsubscribe_candidate(
                sender_domain=domain,
                sender_email=e["from_email"],
                display_name=e.get("display_name") or "",
                unsubscribe_url=e.get("unsubscribe_url"),
                unsubscribe_mailto=e.get("unsubscribe_mailto"),
                one_click=e.get("one_click", False),
            )

    by_sender = defaultdict(list)
    for r in outrecs:
        if r["id"] in inrecs:
            by_sender[inrecs[r["id"]]["from"]].append(r)

    trash_final, holdback = [], []
    for items in by_sender.values():
        trashables = [r for r in items if r["action"] == "trash"]
        others = [r for r in items if r["action"] != "trash"]
        if trashables and not others and not any(r.get("reason", "").startswith("sender rule:") for r in trashables):
            trashables.sort(key=lambda r: inrecs[r["id"]]["date"])
            holdback.append(trashables[0])
            trash_final.extend(trashables[1:])
        else:
            trash_final.extend(trashables)

    keep_mixed = [r for r in outrecs if r["id"] in inrecs and r["action"] in ("keep", "mixed")]
    needs_attn = sum(1 for r in keep_mixed if r.get("needs_attention") is True)

    msgid_to_uid = {e["message_id"]: e["uid"] for e in envs}

    idx, subj_idx = defaultdict(list), defaultdict(list)
    for e in envs:
        idx[(norm(e["subject"]), e["from_email"].lower())].append(e)
        subj_idx[norm(e["subject"])].append(e)

    msgids, unmatched = {}, []
    for r in keep_mixed + holdback + trash_final:
        rec = inrecs[r["id"]]
        if r["id"] in msgid_to_uid:
            msgids[r["id"]] = r["id"]
            continue
        cands = idx.get((norm(rec["subject"]), rec["from"].lower()), [])
        if not cands:
            # from_email parse can fail on odd headers; fall back to subject-only.
            cands = subj_idx.get(norm(rec["subject"]), [])
        if len(cands) == 1:
            msgids[r["id"]] = cands[0]["message_id"]
        elif len(cands) > 1:
            target = parse_jmap(rec["date"])
            best = min(cands, key=lambda e: abs((parse_internaldate(e["internaldate"]) - target).total_seconds()))
            msgids[r["id"]] = best["message_id"]
        else:
            unmatched.append(r["id"])

    ops = []
    for r in keep_mixed:
        if r.get("action") == "mixed":
            r["retention"] = "pending-review"
        if r.get("retention") == "pending-review" and r["id"] not in unmatched:
            tahor_db.queue_message_review(mailbox, msgids[r["id"]], inrecs[r["id"]]["subject"])
        if r["id"] in unmatched:
            continue
        add = [f"category-{r.get('category', 'marketing')}", f"retention-{r.get('retention', 'pending-review')}"]
        if r.get("expense_type") and r["expense_type"] != "n/a":
            add.append(f"expense-{r['expense_type']}")
        if r.get("needs_attention") is True:
            add.append("needs-attention")
        ops.append({"mailbox": mailbox, "message_id": msgids[r["id"]], "uid": msgid_to_uid.get(msgids[r["id"]]), "add": add})
    for r in trash_final:
        if r["id"] not in unmatched:
            ops.append({"mailbox": mailbox, "message_id": msgids[r["id"]],
                        "uid": msgid_to_uid.get(msgids[r["id"]]),
                        "add": ["category-marketing", "retention-transient"]})
    for r in holdback:
        if r["id"] not in unmatched:
            ops.append({
                "mailbox": mailbox,
                "message_id": msgids[r["id"]],
                "uid": msgid_to_uid.get(msgids[r["id"]]),
                "add": ["retention-forever", f"category-{r.get('category', 'marketing')}"],
            })

    Path(f"{prefix}_trash_ids.json").write_text(json.dumps([r["id"] for r in trash_final]))
    Path(f"{prefix}_ops.json").write_text(json.dumps(ops, indent=1))

    print(f"total={len(outrecs)} trash_final={len(trash_final)} holdback={len(holdback)} "
          f"keep_mixed={len(keep_mixed)} needs_attn={needs_attn} unmatched={len(unmatched)}")
    return {op["message_id"]: next((rid for rid, mid in msgids.items() if mid == op["message_id"]), op["message_id"]) for op in ops}



if __name__ == "__main__":
    main()
