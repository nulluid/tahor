# Contributing

Start with the [architecture](README.md#how-it-works), then run the sample-data
preview to see how the decisions fit together.

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m unittest discover -s tests -v
venv/bin/python demo.py
```

Use synthetic messages and disposable databases in tests. Do not add real email,
sender lists, prompts, credentials, or production screenshots to a pull request.
A mailbox mutation needs a test for failure and retry as well as success.

Keep classification, tagging, filing, and retention independently testable.
Preserve configurable filing delays (read: three days, unread: seven days) without
using them to delay classification or deletion. Drafts are never sent automatically. Changes to IMAP writes should identify exactly which UIDs can
be changed and how partial failure is handled.

For UI changes, test authenticated and unauthenticated requests, form-token checks,
invalid input, and the actual persisted outcome. Refresh synthetic screenshots
with `scripts/capture_screenshots.py` when visible behavior changes, inspect the
resulting images, and update any affected README claims in the same change.

For setup changes, test a fresh temporary instance and a repeated setup that must
preserve existing private data. Update the configuration reference and explain
any migration needed by existing installations.

Keep a pull request focused on one concrete behavior. Include the problem, what
changes for the user, and the checks you ran.
