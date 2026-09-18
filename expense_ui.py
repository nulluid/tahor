"""Private calendar-year expense review, with separate currencies and evidence."""
import calendar
from datetime import datetime
from html import escape
from decimal import Decimal


def money(value, currency):
    import business_ledger
    if value is None or currency not in business_ledger.UNITS:
        return 'Needs review'
    return currency + ' ' + format(Decimal(value) / (10 ** business_ledger.UNITS[currency]), '.' + str(business_ledger.UNITS[currency]) + 'f')


def _totals(groups):
    if not groups:
        return '<p>No confirmed amounts in totals.</p>'
    rows = []
    for group in groups:
        label = group.get('category') or group.get('business_key') or ''
        currency = group['currency']
        rows.append('<tr><th scope="row">' + escape(label) + '</th>' + ''.join('<td>' + escape(money(group.get(key, 0), currency)) + '</td>' for key in ('receipts_minor','refunds_minor','net_paid_minor','invoices_minor')) + '</tr>')
    return '<div style="overflow-x:auto"><table class="expense-totals"><thead><tr><th>Category or business</th><th>Charges</th><th>Refunds / credits</th><th>Net paid</th><th>Invoices</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'


def _card(item, year):
    import business_ledger
    import json
    identifier = int(item['id'])
    try:
        proposed = json.loads(item.get('ai_suggestions') or '{}')
    except (ValueError, TypeError):
        proposed = {}
    if not isinstance(proposed, dict): proposed = {}
    financial_reviewed = bool(item.get('owner_confirmed'))
    metadata_reviewed = bool(item.get('metadata_confirmed'))
    def value(field, actual=''):
        if financial_reviewed: return actual or ''
        return proposed.get(field) or actual or ''
    category = item.get('category') or ('' if metadata_reviewed else proposed.get('category') or item.get('category_suggestion')) or ''
    comment = item.get('comment') or ('' if metadata_reviewed else proposed.get('comment')) or ''
    actual_amount = '' if item['amount_minor'] is None or item['currency'] not in business_ledger.UNITS else money(abs(item['amount_minor']),item['currency']).split(' ',1)[1]
    amount = value('amount', actual_amount or item.get('amount_candidate'))
    currency = value('currency',item['currency'])
    kind = value('document_type',item['document_type'])
    vendor = value('vendor',item['vendor'])
    shown = money(item['amount_minor'], item['currency'])
    if item['amount_minor'] is None and amount:
        shown = ((currency + ' ') if currency else '') + amount + ' — needs confirmation'
    messages = {'amount_missing':'The payment amount has not been extracted yet.', 'currency_unconfirmed':'Confirm the currency before this amount enters totals.', 'amount_or_currency_ambiguous':'The amount or currency needs confirmation.', 'document_type_unconfirmed':'Confirm whether this is a receipt, invoice, or completed refund.', 'date_missing_or_ambiguous':'Confirm the document date.', 'possible_duplicate_document':'This may duplicate another transaction.', 'payment_reminder_not_receipt':'This is a payment reminder, not a receipt.', 'noncash_credit_requires_review':'This appears to be noncash credit; verify it before including it in paid totals.'}
    details = ''.join('<p>' + escape(messages.get(reason, reason.replace('_',' '))) + '</p>' for reason in item['review_reasons'])
    reason = item.get('ai_suggestion_reason') or item.get('category_suggestion_reason') or ''
    if proposed:
        details += '<p><strong>AI review ready.</strong> Suggested values are prefilled for review. Your saved values are preserved; totals change only when ledger values are confirmed.</p>'
    if reason: details += '<p>' + escape(reason) + '</p>'
    if item['duplicate_of']:
        details += '<p>Possible duplicate of entry ' + str(int(item['duplicate_of'])) + '; excluded from totals.</p>'
    hidden = f'<input type="hidden" name="year" value="{year}">'
    kinds = '<option value="">Choose document type</option>' + ''.join(f'<option value="{choice}"{" selected" if choice == kind else ""}>{choice.title()}</option>' for choice in business_ledger.KINDS)
    currencies = ''.join(f'<option value="{code}"{" selected" if code == currency else ""}>{code}</option>' for code in business_ledger.UNITS)
    details += (f'<form method="post" action="/expenses/{identifier}/confirm">{hidden}'
                f'<label>Vendor <input name="vendor" maxlength="500" value="{escape(vendor)}" required></label>'
                f'<label>Document type <select name="document_type" required>{kinds}</select></label>'
                f'<label>Currency <select name="currency" required><option value="">Choose currency</option>{currencies}</select></label>'
                f'<label>Amount <input type="text" name="amount" value="{escape(amount)}" inputmode="decimal" required></label>'
                '<p class="hint">Enter the absolute amount. A completed refund is recorded as a negative amount.</p>'
                f'<label>Document date <input type="date" name="document_date" value="{escape(value("document_date",item["document_date"]))}" required></label>'
                f'<label>Receipt or invoice reference <input name="reference" maxlength="80" value="{escape(value("reference",item["reference"]))}"></label>'
                f'<label>Category <input name="category" maxlength="120" value="{escape(category)}" placeholder="Uncategorized"></label>'
                f'<label>Comment / business purpose <textarea name="comment" rows="2" maxlength="4000">{escape(comment)}</textarea></label>'
                + ('<label><input type="checkbox" name="distinct_document" value="1" required> I verified this is a separate transaction, not a duplicate.</label>' if item['duplicate_of'] else '')
                + f'<div class="actions"><button type="submit">Confirm expense</button><button type="submit" formaction="/expenses/{identifier}/metadata" formnovalidate>Save category and comment only</button></div></form>')
    details += f'<form method="post" action="/expenses/{identifier}/suggest-category">{hidden}<button>Suggest all fields with AI</button></form>'
    details += f'<form method="post" action="/expenses/{identifier}/exclude">{hidden}<button type="submit">Remove from Expenses</button><span class="hint"> Removes this entry from the page and totals. Also excludes it from accounting downloads. Its filed email and private recovery record are preserved.</span></form>'
    actual_category = item.get('category') or (category + ' (suggested)' if category else 'Uncategorized')
    summary = f'<span>{escape(item["document_date"] or "Date needs review")}</span> · <strong>{escape(item["vendor"])}</strong> · {escape(shown)} · {escape(actual_category)} · {escape(item["status"].replace("_", " "))}'
    return f'<details class="card expense-row" id="expense-{identifier}" style="padding:.7rem 1rem;margin:.5rem 0"><summary>{summary}</summary><p>{escape(item["document_type"])}{" · email date" if item["date_basis"] == "email_date" else ""}</p><p><a href="/expenses/message/{identifier}">View receipt email</a></p>{details}</details>'


def render(entries, totals, *, year=None, report=None, years=None, archive_status=None):
    year = year or datetime.now().year
    report = report or {'months':[], 'categories':[], 'monthly_categories':[]}
    years = sorted(set(years or [year]) | {year}, reverse=True)
    options = ''.join(f'<option value="{value}"{" selected" if value == year else ""}>{value}</option>' for value in years)
    body = '<style>.expense-totals{width:100%;border-collapse:collapse}.expense-totals th,.expense-totals td{padding:.45rem .65rem;text-align:left;white-space:nowrap;border-bottom:1px solid var(--rule)}.expense-row>summary{line-height:1.6;cursor:pointer}</style><h1>Business expenses</h1><p>Review receipt evidence, track categories and comments, and export original emails. Only confirmed amounts enter totals; possible duplicates and removed entries do not. Refunds reduce net paid. Invoices and currencies remain separate.</p>'
    body += f'<form method="get" action="/expenses"><label>Calendar year <select name="year" onchange="this.form.submit()">{options}</select></label><noscript><button>Show year</button></noscript></form>'
    body += f'<p><a href="/expenses.csv?year={year}">Download {year} accounting CSV</a> · <a href="/expenses.zip?year={year}">Download {year} accounting ZIP</a> · <a href="/expenses.zip?scope=all&amp;purpose=records">Download all records (includes excluded items)</a></p>'
    body += '<details><summary>About downloads and backups</summary><p class="hint">Accounting downloads contain only ready receipts and refunds, excluding removed items, possible duplicates, unpaid invoices, and items awaiting review. Remove from Expenses any other-business costs or non-expenses; this does not delete their emails. The separate all-records download is for recovery, not accounting, and includes excluded items. ZIPs contain the ledger, comments, categories, audit records, and available original .eml files, named by month, category, vendor, and entry ID. The manifest lists any originals still awaiting archive. Automatic recovery backups include this data; configure an off-server destination using the recovery instructions.</p></details>'
    if archive_status:
        body += '<p>Original emails archived: ' + str(archive_status['archived']) + ' of ' + str(archive_status['total']) + '. ' + ('All receipt originals are available.' if archive_status['complete'] else 'Some originals are still awaiting recovery; exports explicitly list missing files.') + '</p>'
    active = [item for item in entries if item['status'] != 'excluded']
    by_month = {}
    for item in active:
        raw_date = item.get('document_date') or item.get('received_at') or ''
        key = raw_date[:7] if len(raw_date) >= 7 else 'undated'
        by_month.setdefault(key, []).append(item)
    for month in range(1,13):
        key = f'{year}-{month:02d}'
        month_entries = by_month.get(key, [])
        groups = [g for g in report.get('months',[]) if g['month']==key]
        categories = [g for g in report.get('monthly_categories',[]) if g['month']==key]
        if not month_entries and not groups:
            continue
        body += f'<section id="month-{month:02d}"><h2>{calendar.month_name[month]} {year}</h2>' + _totals(groups)
        body += '<details><summary>Totals by category</summary>' + _totals(categories) + '</details>'
        body += ''.join(_card(item,year) for item in month_entries) if month_entries else '<p>No expenses recorded for this month.</p>'
        body += '</section>'
    if by_month.get('undated'):
        body += '<section><h2>Date needs review</h2>' + ''.join(_card(item,year) for item in by_month['undated']) + '</section>'
    body += f'<section id="year-total"><h2>{year} total</h2>' + _totals(totals) + '<details><summary>Year totals by category</summary>' + _totals(report.get('categories',[])) + '</details></section>'
    if not active:
        body += '<p>No active expenses for this year. Removed entries stay out of accounting downloads; their emails and recovery records are preserved.</p>'
    return body
