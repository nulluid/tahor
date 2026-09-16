import json
import os
from pathlib import Path

IMAP_HOST = os.environ.get("FASTMAIL_HOST", "imap.fastmail.com")
IMAP_PORT = 993
SMTP_HOST = os.environ.get("FASTMAIL_SMTP_HOST", "smtp.fastmail.com")
SMTP_PORT = 465


def email_address():
    value = os.environ.get("FASTMAIL_EMAIL")
    if not value:
        raise SystemExit("Set FASTMAIL_EMAIL in your environment.")
    return value


def app_password():
    value = os.environ.get("FASTMAIL_APP_PASSWORD")
    if not value:
        raise SystemExit("Set FASTMAIL_APP_PASSWORD in your environment.")
    return value


def filing_root():
    return os.environ.get("FILING_ROOT", "Filed")


def retention_days(tier, default):
    return int(os.environ.get(f"RETENTION_{tier.upper()}_DAYS", default))


def filing_min_age_days(status, default):
    return int(os.environ.get(f"FILING_{status.upper()}_MIN_AGE_DAYS", default))


def vendor_buckets():
    path = os.environ.get("VENDOR_BUCKETS_PATH", str(Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent)) / "vendor_buckets.json"))
    with open(path) as f:
        raw = json.load(f)
    return {key.lower(): tuple(value) for key, value in raw.items()}
