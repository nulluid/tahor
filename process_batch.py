#!/usr/bin/env python3
"""
Turn one batch's classification output into the two files the rest of the
pipeline needs: a trash list and a keyword_tool.py ops file. Trash operations
apply classification tags before deleting the targeted message.

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
import reply_rules
import coupon_expiry
import business_filing


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

    business_rules = business_filing.load_rules()
    coupon_policies = coupon_expiry.policies()
    active_reply_rules = {r["id"]: r for r in reply_rules.get_rules()}
    for r in outrecs:
        rec = inrecs.get(r["id"])
        if not rec or r.get("action") == "error":
            continue
        versions = r.get('reply_rule_versions')
        expected_versions = {key: rule.get('revision', key) for key, rule in active_reply_rules.items()}
        if (active_reply_rules or versions is not None) and versions != expected_versions:
            r.update(action='error', reason='Reply rules changed during classification; retry required')
            continue
        domain = sender_domain_of(rec["from"])
        rule = tahor_db.get_sender_rule(domain)
        if rule == "block_all" or (rule == "block_marketing" and r.get("category") == "marketing"):
            r["action"] = "trash"
            r["reason"] = f"sender rule: {rule}"

        if rule not in ('block_all', 'block_marketing') and coupon_expiry.policy_for(rec.get('from'), coupon_policies) and r.get('category') == 'marketing':
            unsubscribe = tahor_db.get_unsubscribe_candidate(domain)
            if not unsubscribe or unsubscribe['status'] != 'unsubscribed':
                coupon_expiry.protect_result(r, rec.get('from'), rec.get('coupon_source', ''), coupon_policies)

        matches = [key for key in r.get('reply_rule_matches', []) if key in active_reply_rules]
        uncertain = [key for key in r.get('reply_rule_uncertain', []) if key in active_reply_rules]
        if r.get('action') != 'error' and (matches or uncertain):
            # Reply protection takes precedence over an old marketing/domain block.
            if r['action'] == 'trash':
                r.update(action='keep', category='personal-correspondence', retention='standard', expense_type='n/a', needs_attention=False)
            if r.get('retention') in ('transient', 'brief'):
                r['retention'] = 'standard'
            if matches and not uncertain:
                r['action'] = 'keep'
                if r.get('retention') == 'pending-review':
                    r['retention'] = 'standard'
            # Draft eligibility is separate from a keep/trash decision. An
            # uncertain reply match protects mail but never creates a review hold.
            r['reply_rule_matches'], r['reply_rule_uncertain'] = matches, uncertain
            for rule_id in matches:
                tahor_db.record_reply_rule_match(rule_id, r['id'], rec['from'])

        # Business receipt protection is applied before constructing any trash
        # operation; private source/date rules never classify all merchant ads as receipts.
        business_filing.protect_classification(r, rec, rules=business_rules)

    classifications = {row["id"]: row for row in outrecs}
    for e in envs:
        if classifications.get(e.get("message_id"), {}).get("action") == "error":
            continue
        if not (e.get("unsubscribe_url") or e.get("unsubscribe_mailto")):
            continue
        domain = sender_domain_of(e["from_email"])
        if domain and tahor_db.get_sender_rule(domain) is None:
            try:
                received_at = parse_internaldate(e.get("internaldate", "")).isoformat()
            except (ValueError, TypeError):
                received_at = None
            tahor_db.upsert_unsubscribe_candidate(
                sender_domain=domain,
                sender_email=e["from_email"],
                display_name=e.get("display_name") or "",
                unsubscribe_url=e.get("unsubscribe_url"),
                unsubscribe_mailto=e.get("unsubscribe_mailto"),
                one_click=e.get("one_click", False),
                message_id=e.get("message_id"),
                received_at=received_at,
                is_marketing=classifications.get(e.get("message_id"), {}).get("category") == "marketing",
                metadata={"mailbox": mailbox, "uid": e.get("uid"), "uidvalidity": e.get("uidvalidity"),
                          "subject": e.get("subject") or inrecs.get(e.get("message_id"), {}).get("subject", ""),
                          "date": inrecs.get(e.get("message_id"), {}).get("date", "")},
            )

    trash_final = [r for r in outrecs if r["id"] in inrecs and r["action"] == "trash"]

    keep_mixed = [r for r in outrecs if r["id"] in inrecs and r["action"] in ("keep", "mixed")]
    needs_attn = sum(1 for r in keep_mixed if r.get("needs_attention") is True)

    msgid_to_uid = {e["message_id"]: e["uid"] for e in envs}
    envelopes_by_id = {e["message_id"]: e for e in envs}

    idx, subj_idx = defaultdict(list), defaultdict(list)
    for e in envs:
        idx[(norm(e["subject"]), e["from_email"].lower())].append(e)
        subj_idx[norm(e["subject"])].append(e)

    msgids, unmatched = {}, []
    for r in keep_mixed + trash_final:
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
            envelope = envelopes_by_id[msgids[r["id"]]]
            tahor_db.queue_message_review(mailbox, msgids[r["id"]], inrecs[r["id"]]["subject"], envelope.get("uid"), envelope.get("uidvalidity"), metadata={"sender": inrecs[r["id"]].get("from", ""), "date": inrecs[r["id"]].get("date", ""), "received_at": parse_internaldate(envelope["internaldate"]).isoformat() if envelope.get("internaldate") else "", "snippet": inrecs[r["id"]].get("snippet", "")})
        if r["id"] in unmatched:
            continue
        add = [f"category-{r.get('category', 'marketing')}", f"retention-{r.get('retention', 'pending-review')}"]
        if r.get('retention') == 'brief':
            # Standard remains the classifier-complete marker; expiry is separate.
            add = [f"category-{r.get('category', 'marketing')}", 'retention-standard', 'retention-short-lived']
        add.extend(r.get('coupon_keywords', []))
        add.extend(r.get('business_keywords', []))
        if r.get("expense_type") and r["expense_type"] != "n/a":
            add.append(f"expense-{r['expense_type']}")
        if r.get("needs_attention") is True:
            add.append("needs-attention")
        if r.get('reply_rule_versions'):
            add.extend(reply_rules.scan_keyword(rule) for rule in active_reply_rules.values())
        if r.get('reply_rule_matches') or r.get('reply_rule_uncertain'):
            add.append(reply_rules.PROTECTED_KEYWORD)
            add.extend(reply_rules.keyword(active_reply_rules[key]) for key in r.get('reply_rule_matches', []) if key in active_reply_rules)
        ops.append({"mailbox": mailbox, "message_id": msgids[r["id"]], "uid": msgid_to_uid.get(msgids[r["id"]]), "add": add, "remove": (["delete-pending", "retention-transient"] if r.get("reply_rule_matches") or r.get("reply_rule_uncertain") else []) + (["delete-pending", "retention-transient", "retention-standard", "expense-personal"] if "business-receipt" in r.get("business_keywords", []) else []) + (["retention-short-lived"] if r.get("retention") != "brief" else [])})
    for r in trash_final:
        if r["id"] not in unmatched:
            ops.append({"mailbox": mailbox, "message_id": msgids[r["id"]],
                        "uid": msgid_to_uid.get(msgids[r["id"]]), "delete": True,
                        "add": [f"category-{r.get('category', 'marketing')}", "retention-transient", "delete-pending"]})

    for op in ops:
        validity = envelopes_by_id[op["message_id"]].get("uidvalidity")
        if validity:
            op["uidvalidity"] = validity
    Path(f"{prefix}_trash_ids.json").write_text(json.dumps([r["id"] for r in trash_final]))
    Path(f"{prefix}_ops.json").write_text(json.dumps(ops, indent=1))

    print(f"total={len(outrecs)} trash_final={len(trash_final)} "
          f"keep_mixed={len(keep_mixed)} needs_attn={needs_attn} unmatched={len(unmatched)}")
    return {op["message_id"]: next((rid for rid, mid in msgids.items() if mid == op["message_id"]), op["message_id"]) for op in ops}



if __name__ == "__main__":
    main()
