#!/usr/bin/env python3
"""
Propose a new Sieve script: commit it to DATA_DIR, then insert a
'sieve_update' row so the decisions page shows a dismissable banner until
you confirm you've pasted it into your mail provider (Sieve can't be
pushed via API).

Usage: python3 propose_sieve_update.py <new_sieve_file> "<one-line reason>"
"""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import os

DB_PATH = Path(__file__).parent / "decisions.db"

# DATA_DIR holds sieve.txt -- the same gitignored file classify.py/config.py
# read from the repo root. Defaults to this repo (one directory up from
# decision-app/), but can point at a separate private git repo if you want
# that data to have its own tracked history independent of this codebase.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
SIEVE_PATH = DATA_DIR / "sieve.txt"


def git(*args):
    subprocess.run(["git", "-C", str(DATA_DIR), *args], check=True)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    new_content = Path(sys.argv[1]).read_text()
    reason = sys.argv[2]

    SIEVE_PATH.write_text(new_content)
    git("add", "sieve.txt")
    result = subprocess.run(["git", "-C", str(DATA_DIR), "diff", "--cached", "--quiet"])
    if result.returncode == 0:
        print("No change from current sieve.txt -- nothing to propose.")
        return
    git("commit", "-m", f"propose sieve update: {reason}")
    git("push", "origin", "main")

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO decisions (kind, summary, context, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        ("sieve_update", "Sieve filter update recommended", reason, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    print(f"Committed and flagged for review: {reason}")


if __name__ == "__main__":
    main()
