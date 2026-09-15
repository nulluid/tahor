#!/usr/bin/env python3
"""
Regenerate the recommended Sieve script from the current sender_rules table
and hand it to propose_sieve_update.py so it gets flagged for you to paste
into Fastmail. Defense in depth: sender rules are already enforced during
classification, but a Sieve-level block also catches mail that arrives
faster than the classification pipeline gets to it.

block_all domains are discarded outright. block_marketing domains are only
discarded when the message also carries a List-Unsubscribe header -- Sieve
has no real way to distinguish marketing from transactional otherwise, so
this errs toward under-blocking (a receipt slips through) rather than
over-blocking (a receipt gets silently dropped).

Usage: python3 generate_sieve.py
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tahor_db

HEADER = 'require ["fileinto", "envelope", "header"];\n\n'


def domain_test(domain):
    return f'address :domain :is "from" "{domain}"'


def build_sieve(block_all, block_marketing):
    parts = [HEADER]
    if block_all:
        tests = ",\n        ".join(domain_test(d) for d in sorted(block_all))
        parts.append(f"if anyof (\n        {tests}\n    ) {{\n    discard;\n    stop;\n}}\n\n")
    if block_marketing:
        tests = ",\n            ".join(domain_test(d) for d in sorted(block_marketing))
        parts.append(
            "if allof (\n"
            f"    anyof (\n            {tests}\n    ),\n"
            '    header :exists "List-Unsubscribe"\n'
            ") {\n    discard;\n    stop;\n}\n"
        )
    return "".join(parts)


def main():
    conn = tahor_db.get_db()
    rows = conn.execute("SELECT sender_domain, rule FROM sender_rules").fetchall()
    block_all = {r["sender_domain"] for r in rows if r["rule"] == "block_all"}
    block_marketing = {r["sender_domain"] for r in rows if r["rule"] == "block_marketing"}

    if not block_all and not block_marketing:
        print("No sender rules set, nothing to generate.")
        return

    sieve_text = build_sieve(block_all, block_marketing)
    tmp_path = Path("/tmp/tahor_sieve_generated.txt")
    tmp_path.write_text(sieve_text)

    reason = f"{len(block_all)} blocked domain(s), {len(block_marketing)} marketing-only block(s)"
    subprocess.run([sys.executable, str(Path(__file__).parent / "propose_sieve_update.py"), str(tmp_path), reason], check=True)


if __name__ == "__main__":
    main()
