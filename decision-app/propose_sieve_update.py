#!/usr/bin/env python3
"""Save a complete, explicitly supplied Sieve proposal for manual installation."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_changes import atomic_write, commit_data
import tahor_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('reason')
    args = parser.parse_args()
    data_dir = Path(os.environ.get('DATA_DIR', Path(__file__).resolve().parent.parent))
    content = args.source.read_text()
    if not content.strip():
        raise SystemExit('Refusing an empty Sieve proposal')
    target = data_dir / 'sieve.txt'
    if target.exists() and target.read_text() == content:
        print('Sieve proposal is unchanged.')
        return
    atomic_write(target, content)
    conn = tahor_db.get_db()
    try:
        with conn:
            conn.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES ('sieve_update','Review Sieve proposal',?,'pending',?)", (args.reason, datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()
    commit_data(data_dir, 'update Sieve proposal', ['sieve.txt'])
    print('Proposal saved for review. No provider filter was changed.')


if __name__ == '__main__':
    main()
