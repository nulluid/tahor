#!/usr/bin/env bash
# Get Tahor running on a fresh Linux box. Reference target is an Oracle
# Cloud Always Free instance (Ampere A1 or E2.1.Micro, Oracle Linux or
# Ubuntu), but nothing here is Oracle-specific -- any Linux with python3
# works.
#
# Safe to re-run: it never overwrites an existing .env/vendor_buckets.json/
# prompt.txt, and venv creation and pip installs are idempotent.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found." >&2
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        case "${ID:-}${ID_LIKE:-}" in
            *debian*|*ubuntu*)
                echo "Install it with: sudo apt update && sudo apt install -y python3 python3-venv" >&2
                ;;
            *rhel*|*fedora*|*ol*)
                echo "Install it with: sudo dnf install -y python3" >&2
                ;;
            *)
                echo "Install python3 with your distro's package manager." >&2
                ;;
        esac
    else
        echo "Install python3 with your distro's package manager." >&2
    fi
    exit 1
fi

# Only the decision-app needs Flask/requests -- classify.py, fetch_batch.py,
# backlog_worker.py, and the sweeps are standard-library-only and run fine
# with plain python3, no venv required.
if [ ! -d venv ]; then
    echo "Creating venv for the decision-app..."
    python3 -m venv venv
fi
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet flask requests

copy_if_missing() {
    src="$1"
    dst="$2"
    if [ -f "$dst" ]; then
        echo "$dst already exists, leaving it alone."
    elif [ -f "$src" ]; then
        cp "$src" "$dst"
        echo "Created $dst from $src."
    fi
}

copy_if_missing .env.example .env
copy_if_missing vendor_buckets.example.json vendor_buckets.json
copy_if_missing prompt.example.txt prompt.txt

UNIT_FILE="tahor-backlog-worker.service.example"
cat > "$UNIT_FILE" <<'EOF'
[Unit]
Description=Tahor backlog worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=REPLACE_WITH_YOUR_USER
WorkingDirectory=REPLACE_WITH_REPO_PATH
EnvironmentFile=REPLACE_WITH_REPO_PATH/.env
ExecStart=/usr/bin/python3 REPLACE_WITH_REPO_PATH/backlog_worker.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
echo "Wrote $UNIT_FILE (example only -- not installed)."

cat <<'EOF'

Next steps:
  1. Edit .env with your real IMAP host/address/app-password.
  2. Edit vendor_buckets.json and prompt.txt for your own mail.
  3. Pick a CLASSIFY_BACKEND (local/gemini/openrouter/openrouter-free) --
     see classify.py's docstring.
  4. Run a batch by hand first to confirm it works end to end:
       python3 fetch_batch.py INBOX current_batch
       python3 classify.py current_batch_in.json current_batch_out.json prompt.txt
       python3 process_batch.py current_batch INBOX
       python3 keyword_tool.py current_batch_ops.json
  5. Only once that looks right, set up ongoing automation:
       - Edit tahor-backlog-worker.service.example, then as root:
           sudo cp tahor-backlog-worker.service.example /etc/systemd/system/tahor-backlog-worker.service
           sudo systemctl daemon-reload
           sudo systemctl enable --now tahor-backlog-worker
         (installing a systemd unit needs root -- this script deliberately
         does not do it for you)
       - Or cron, if you'd rather run classify.py by hand on a schedule
         instead of the always-on worker; see README.md.
  6. Optional: the decision-app, for resolving ambiguous cases from a
     browser. See README.md for OAuth setup, then:
       venv/bin/python3 decision-app/app.py
EOF
