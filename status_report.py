#!/usr/bin/env python3
"""Read worker health without sending mail or contacting model providers."""
import argparse
import json
import runtime_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    snapshot = runtime_status.read_status()
    description = runtime_status.describe_status(snapshot)
    print(json.dumps(snapshot, indent=2) if args.json else description)
    if args.check and (not snapshot or '30 minutes' in description or 'could not' in description or snapshot.get('state') in ('error', 'retrying')):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
