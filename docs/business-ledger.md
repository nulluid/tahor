# Private business receipt ledger

Tahor can retain a reviewable ledger alongside business receipt filing. Only messages matched to an enabled private business rule and verified against their mailbox identity enter this ledger automatically. Ordinary personal receipts are not included merely because a company sent them.

The ledger records the business key, vendor, source message location, document date, currency, and an explicit amount when it can extract one safely. The ledger stores a hash of the source text. A separate private archive stores the original email, including MIME attachments, as supporting evidence.

## Amounts and review

The extractor reads explicit totals in plain text and flattened email tables, such as `Amount paid: USD 123.45`. A bare-dollar amount is prefilled for review while its currency remains unconfirmed. It does not estimate missing amounts, infer a currency from `$`, convert currencies, or round extra decimal places. Conflicting totals, ambiguous formatting, missing dates, and unconfirmed document types require review and are excluded from summaries.

The supported currencies are USD, EUR, GBP, CAD, AUD, NZD, CHF, SEK, NOK, DKK, JPY, KRW, KWD, and BHD. Amounts are stored as integer minor units, respecting each supported currency's precision. Other currencies require a future extractor extension; they are not silently treated as two-decimal currencies.

Receipt payments, refunds, and invoice documents are separate:

- Paid receipts contribute to paid totals.
- Refunds contribute negative amounts to the same currency's paid total.
- Invoice totals are shown separately. They are not an outstanding balance, and an invoice plus its payment receipt is not counted as two paid expenses.

An explicit ISO document date takes precedence. When only the email's date is available, the entry is labeled `email_date`; that is not a claim about the date of purchase. Owner-confirmed dates are labeled `owner`.

Repeated scans of the same business, sender, and Message-ID update the source location without adding another expense or overwriting owner corrections. A repeated document reference, or identical source text for the same date, is flagged as a possible duplicate. Such entries stay out of totals until the owner excludes the duplicate or explicitly confirms that it is a distinct transaction. Different invoices and receipts remain separate document types.

This ledger does not determine tax deductibility, allocate business versus personal portions, reconcile bank balances, or replace accounting review. Check amounts and supporting documents before using an export for bookkeeping.

## Year view and categories

Expenses defaults to the current year. Choose another calendar year to see its monthly rows and totals. All rows are compact and expand for editing; confirmed items remain editable. Monthly and yearly summaries expand to show category totals, with refunds reducing net paid in their own currency. Upcoming-payment reminders are correspondence, not paid receipts. Pending refunds and noncash store credit do not automatically reduce paid totals.

AI attempts every editable field: vendor, document date, type, reference, amount, currency, bookkeeping category, and a concise business-purpose comment. Suggestions use the configured rule-writing models and privacy policy with verified receipt body text and private owner guidance. Attachments are preserved in the archive but are not sent for these suggestions. Unknown values remain blank rather than invented.

Suggestions are stored separately from the ledger values and prefill unconfirmed fields for review. Confirm expense saves the reviewed values together; Save category and comment only leaves the amounts unconfirmed. Your explicit metadata choices, including blank fields, are preserved. Conventional bookkeeping categories include Software subscriptions, Hosting, Computer equipment, Office supplies, Professional services, and Travel. These labels do not automatically determine tax treatment or deductibility. Uncategorized amounts remain visible under Uncategorized.

Remove from Expenses immediately hides an entry and excludes it from totals. The original email and before-and-after history remain in private storage and exports. The background expense worker runs every five minutes; temporary capture or category failures retry.

## Export and recovery

The ledger, owner-review history, and immutable original-email archive are tables in Tahor's private `decisions.db`. Existing database snapshots and the [off-host recovery schedule](private-backups.md) therefore back them up together. Recovery validates the snapshot before restoring it. An automatic off-host schedule must be configured on a separate trusted device; a backup on the Tahor server alone does not protect against losing that server. Keep originals, exported files, private business rules, and recovery copies out of the public source repository.

The Expenses page offers accounting CSV and ZIP downloads for the selected year. These contain only ready receipts and refunds, excluding removed entries, possible duplicates, unpaid invoices, and items awaiting review. Use **Remove from Expenses** for non-expenses and costs belonging to another business; the filed email stays saved. A bill and its paid receipt are not both counted: unpaid invoices are omitted from accounting downloads. Duplicate receipt detection still depends on matching source identities or document references; review unrelated-looking copies rather than assuming identical amounts prove a duplicate.

**Download all records (includes excluded items)** is a separate recovery ZIP, not an accounting report. It retains all years and all review states. Each ZIP contains:

- `expenses.csv`, including review states and owner annotations.
- `ledger.json`, retaining the complete exported ledger records.
- `reviews.json`, with before-and-after changes for those records.
- Original `.eml` files with attachments, named `YYYY-MM-category-vendor-entryID.eml`.
- `manifest.json`, mapping entry IDs to filenames and SHA-256 checksums.

The background worker captures originals without marking messages read. Before saving, it checks the saved mailbox generation and UID, exact sender and message identity, complete byte length, and the source-content hash. A different original cannot overwrite an archived copy. Downloads use this local private archive and do not perform mailbox searches while the browser waits.

The page reports missing originals. If any source has not been captured or fails its checksum, the ZIP is explicitly marked incomplete in its README and manifest; its ledger rows remain included. Do not treat an incomplete bundle as a complete set of supporting documents. Background capture retries missing sources, and the original email remains in the mailbox.

This version limits each original email to **50 MiB**, the complete original-email archive to **512 MiB**, and the uncompressed accounting export payload to **512 MiB**. These limits bound memory, disk, and recovery-transfer costs. Reaching the archive limit does not remove any existing evidence or mailbox message; further captures remain pending. Exports over the download limit fail with an explicit error; select a single year to reduce their size. Larger archives require a future storage-capacity extension rather than silently dropping receipts.

From an installed checkout, generate a full-records private CSV file (including excluded and unreviewed records, for recovery rather than accounting) using the same environment file as the running service:

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
