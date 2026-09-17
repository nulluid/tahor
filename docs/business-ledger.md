# Private business receipt ledger

Tahor can retain a reviewable ledger alongside business receipt filing. Only messages matched to an enabled private business rule and verified against their mailbox identity enter this ledger automatically. Ordinary personal receipts are not included merely because a company sent them.

The ledger records the business key, vendor, source message location, document date, currency, and an explicit amount when it can extract one safely. It stores a hash of the source text rather than a copy of the email body. The original email remains the supporting document.

## Amounts and review

The initial extractor reads explicit totals in plain text, such as `Amount paid: USD 123.45`. It does not estimate missing amounts, infer a currency from `$`, convert currencies, or round extra decimal places. Conflicting totals, ambiguous formatting, missing dates, and unconfirmed document types require review and are excluded from summaries.

The supported currencies are USD, EUR, GBP, CAD, AUD, NZD, CHF, SEK, NOK, DKK, JPY, KRW, KWD, and BHD. Amounts are stored as integer minor units, respecting each supported currency's precision. Other currencies require a future extractor extension; they are not silently treated as two-decimal currencies.

Receipt payments, refunds, and invoice documents are separate:

- Paid receipts contribute to paid totals.
- Refunds contribute negative amounts to the same currency's paid total.
- Invoice totals are shown separately. They are not an outstanding balance, and an invoice plus its payment receipt is not counted as two paid expenses.

An explicit ISO document date takes precedence. When only the email's date is available, the entry is labeled `email_date`; that is not a claim about the date of purchase. Owner-confirmed dates are labeled `owner`.

Repeated scans of the same business, sender, and Message-ID update the source location without adding another expense or overwriting owner corrections. A repeated document reference, or identical source text for the same date, is flagged as a possible duplicate. Such entries stay out of totals until the owner excludes the duplicate or explicitly confirms that it is a distinct transaction. Different invoices and receipts remain separate document types.

This ledger does not determine tax deductibility, allocate business versus personal portions, reconcile bank balances, or replace accounting review. Check amounts and supporting documents before using an export for bookkeeping.

## Export and recovery

The ledger and its owner-review history are tables in Tahor's private `decisions.db`. Existing private database backups therefore include them. Keep exported files and recovery copies out of the public source repository.

From an installed checkout, generate a new private CSV file using the same environment file as the running service:

```sh
python business_ledger.py --env ~/.config/tahor/config.env \
  --csv ~/tahor-expenses.csv
```

Add `--business example-services` to select one configured business key. Omit `--csv` to print grouped currency summaries and the count of entries needing review. The command creates exports with mode `0600` and refuses to overwrite an existing file. Exported text cells are protected against spreadsheet formula injection; computed numeric amounts remain numeric.

CSV includes review status, duplicate references, the date source, and the message's mailbox identity. It contains no email bodies. There is no Google Drive upload or cloud spreadsheet integration in this version.

## Integration boundary

The filing worker calls `business_ledger.record_receipt(metadata, text, verified_business=True)` only after verifying the configured business match and the original message's UID, UIDVALIDITY, and Message-ID. A caller must supply the private business key and matching rule identifier; a model's unverified assertion alone does not satisfy this boundary.

Owner-facing integrations can use `list_entries`, `get_entry`, `summaries`, and `export_csv`. `confirm_entry` supplies reviewed values; `exclude_entry` retains a document while omitting it from totals. Those mutations must sit behind owner authentication and CSRF protection. They record before-and-after values privately for recovery and review.

If the same business, sender, and Message-ID appears with different message text, Tahor preserves the original source identity and excludes that entry from totals for source reconciliation. Ordinary amount confirmation, including the distinct-document checkbox, cannot override this identity conflict. You can exclude the entry while investigating it.
