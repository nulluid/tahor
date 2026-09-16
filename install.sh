#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null || { echo 'Install Python 3.9 or newer first.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else "Python 3.9+ is required")'
if [ ! -d venv ]; then
  python3 -m venv venv
fi
venv/bin/python -m pip install --quiet -r requirements.txt
venv/bin/python setup_tahor.py "$@"
printf '\nSetup complete. Check configuration with:\n  venv/bin/python run.py doctor\n\nStart the worker with:\n  venv/bin/python run.py worker\n\nWeb app and unattended services: see docs/setup.md.\n'
