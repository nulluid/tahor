"""Private calendar-year expense review, with separate currencies and evidence."""
import calendar
from datetime import datetime
from html import escape
from decimal import Decimal
from urllib.parse import urlencode
import json


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
        label = group.get('category') or group.get('business_name') or group.get('business_key') or ''
        currency = group['currency']
        rows.append('<tr><th scope="row">' + escape(label) + '</th>' + ''.join('<td>' + escape(money(group.get(key, 0), currency)) + '</td>' for key in ('receipts_minor','refunds_minor','net_paid_minor')) + '</tr>')
    return '<div style="overflow-x:auto"><table class="expense-totals"><thead><tr><th>Category or business</th><th>Business-share charges</th><th>Refunds / credits</th><th>Net business spending</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'


def _card(item, year, business=None, profiles=None, accounting=None):
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
    messages = {'amount_missing':'The payment amount has not been extracted yet.', 'currency_unconfirmed':'Confirm the currency before this amount enters totals.', 'amount_or_currency_ambiguous':'The amount or currency needs confirmation.', 'document_type_unconfirmed':'Confirm whether this is a paid receipt or completed refund.', 'date_missing_or_ambiguous':'Confirm the document date.', 'possible_duplicate_document':'This may duplicate another transaction.', 'payment_reminder_not_receipt':'This is a payment reminder, not a receipt.', 'noncash_credit_requires_review':'This appears to be noncash credit; verify it before including it in paid totals.'}
    details = ''.join('<p>' + escape(messages.get(reason, reason.replace('_',' '))) + '</p>' for reason in item['review_reasons'])
    reason = item.get('ai_suggestion_reason') or item.get('category_suggestion_reason') or ''
    if proposed:
        details += '<p><strong>AI review ready.</strong> Suggested values are prefilled for review. Your saved values are preserved; totals change only when ledger values are confirmed.</p>'
    if reason: details += '<p>' + escape(reason) + '</p>'
    if item['duplicate_of']:
        details += '<p>Possible duplicate of entry ' + str(int(item['duplicate_of'])) + '; excluded from totals.</p>'
    hidden = _hidden(year, business)
    kinds = '<option value="">Choose document type</option>' + ''.join(f'<option value="{choice}"{" selected" if choice == kind else ""}>{choice.title()}</option>' for choice in business_ledger.KINDS)
    currencies = ''.join(f'<option value="{code}"{" selected" if code == currency else ""}>{code}</option>' for code in business_ledger.UNITS)
    details += (f'<form method="post" action="/expenses/{identifier}/confirm">{hidden}'
                f'<label>Vendor <input name="vendor" maxlength="500" value="{escape(vendor)}" required></label>'
                f'<label>Document type <select name="document_type" required>{kinds}</select></label>'
                f'<label>Currency <select name="currency" required><option value="">Choose currency</option>{currencies}</select></label>'
                f'<label>Amount <input type="text" name="amount" value="{escape(amount)}" inputmode="decimal" required></label>'
                '<p class="hint">Enter the absolute amount. A completed refund is recorded as a negative amount.</p>'
                f'<label>Document date <input type="date" name="document_date" value="{escape(value("document_date",item["document_date"]))}" required></label>'
                f'<label>Receipt reference <input name="reference" maxlength="80" value="{escape(value("reference",item["reference"]))}"></label>'
                f'<label>Category <input name="category" maxlength="120" value="{escape(category)}" placeholder="Uncategorized"></label>'
                f'<label>Comment / business purpose <textarea name="comment" rows="2" maxlength="4000">{escape(comment)}</textarea></label>'
                + ('<label><input type="checkbox" name="distinct_document" value="1" required> I verified this is a separate transaction, not a duplicate.</label>' if item['duplicate_of'] else '')
                + f'<div class="actions"><button type="submit">Confirm expense</button><button type="submit" formaction="/expenses/{identifier}/metadata" formnovalidate>Save category and comment only</button></div></form>')
    details += f'<form method="post" action="/expenses/{identifier}/suggest-category">{hidden}<button>Suggest all fields with AI</button></form>'
    details += f'<form method="post" action="/expenses/{identifier}/exclude">{hidden}<button type="submit">Remove from Expenses</button><span class="hint"> Removes this entry from the page and totals. Also excludes it from accounting downloads. Its filed email and private recovery record are preserved.</span></form>'
    if accounting is not None:
        details += _accounting_form(identifier, accounting, year, business)
    if profiles and len(profiles) > 1:
        choices = ''.join('<option value="' + escape(str(p['key'])) + '"' + (' selected' if p['key'] == business else '') + '>' + escape(p.get('name') or p['key']) + '</option>' for p in profiles)
        details += f'<details class="expense-subpanel"><summary>Move to another business</summary><form method="post" action="/expenses/{identifier}/reassign">{hidden}<label>Business <select name="target_business">{choices}</select></label><p class="hint">Changes which business owns this record. The original email remains saved.</p><button>Move expense</button></form></details>'
    actual_category = item.get('category') or (category + ' (suggested)' if category else 'Uncategorized')
    preview_query = '?' + urlencode({'business': business}) if business else ''
    summary = f'<span>{escape(item["document_date"] or "Date needs review")}</span> · <strong>{escape(item["vendor"])}</strong> · {escape(shown)} · {escape(actual_category)} · {escape(item["status"].replace("_", " "))}'
    return f'<details class="card expense-row" id="expense-{identifier}" style="padding:.7rem 1rem;margin:.5rem 0"><summary>{summary}</summary><p>{escape(item["document_type"])}{" · email date" if item["date_basis"] == "email_date" else ""}</p><p><a href="/expenses/message/{identifier}{escape(preview_query)}">View receipt email</a></p>{details}</details>'



def _hidden(year, business):
    return f'<input type="hidden" name="year" value="{int(year)}">' + ('<input type="hidden" name="business" value="' + escape(str(business)) + '">' if business else '')


_STYLES = '''<style>
main:has(.expense-head){max-width:1080px}
.expense-head{display:flex;align-items:flex-end;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin:1.8rem 0 1rem}.expense-head h1{margin:.2rem 0}.expense-eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.75rem;color:var(--muted);font-weight:700}.expense-subtitle{max-width:65ch;color:var(--muted);line-height:1.6}.expense-filters{display:flex;gap:.7rem;flex-wrap:wrap}.expense-filters label{min-width:8rem}.expense-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.8rem;margin:1.5rem 0}.expense-kpi{padding:1.15rem;border:1px solid var(--rule);border-radius:14px;background:var(--raised)}.expense-kpi h2{margin:0 0 .6rem;font-size:.85rem;font-family:inherit;font-weight:500;color:var(--muted)}.expense-kpi strong{display:block;font-size:1.45rem;line-height:1.5;font-variant-numeric:tabular-nums}.expense-kpi small{display:block;color:var(--muted);margin-top:.5rem}.expense-kpi.primary{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--raised))}.expense-tools{display:flex;align-items:center;flex-wrap:wrap;gap:.6rem 1.1rem;margin:1.1rem 0}.expense-panel{border:1px solid var(--rule);border-radius:14px;padding:1rem 1.2rem;margin:1rem 0}.expense-panel summary,.expense-subpanel summary{cursor:pointer;font-weight:600}.expense-panel p{line-height:1.65}.expense-totals{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}.expense-totals th,.expense-totals td{padding:.6rem .7rem;text-align:left;white-space:nowrap;border-bottom:1px solid var(--rule)}.expense-totals th{font-size:.85rem;color:var(--muted)}.expense-totals td:not(:first-child){text-align:right}.expense-row>summary{line-height:1.6;cursor:pointer}.expense-row[open]{padding-bottom:1rem!important}.expense-row form{margin:1rem 0}.expense-row form:not(:last-child){border-bottom:1px solid var(--rule);padding-bottom:1rem}.expense-form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.7rem 1rem}.expense-form-grid label{margin:0}.expense-full{grid-column:1/-1}.expense-subpanel{border-top:1px solid var(--rule);padding-top:1rem;margin-top:1rem}.expense-note{white-space:pre-wrap;overflow-wrap:anywhere}.expense-month{margin:2rem 0}.expense-month h2{margin-bottom:.6rem}.expense-badge{display:inline-block;border:1px solid var(--rule);border-radius:20px;padding:.15rem .55rem;font-size:.8rem;color:var(--muted)}.expense-empty{padding:2rem;text-align:center;color:var(--muted)}.expense-checklist{padding-left:1.3rem;line-height:1.8}.expense-allocations{display:grid;gap:.6rem}.expense-allocation{display:grid;grid-template-columns:minmax(160px,2fr) minmax(80px,1fr) minmax(140px,2fr);gap:.6rem}.expense-detail-grid{display:grid;align-items:start;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:1rem}.expense-detail-grid>.expense-panel{margin:0}.expense-progress{height:.35rem;border-radius:3px;background:var(--rule);overflow:hidden;margin-top:.8rem}.expense-progress>span{display:block;height:100%;background:var(--accent)}@media(max-width:600px){.expense-allocation{grid-template-columns:1fr}.expense-kpis{grid-template-columns:1fr 1fr}.expense-kpi{padding:.8rem}.expense-kpi strong{font-size:1.1rem}.expense-filters{width:100%}.expense-filters label{flex:1}.expense-panel{padding:.85rem}.expense-totals th,.expense-totals td{padding:.45rem}}
</style>'''


def _currency_values(groups, field):
    return '<br>'.join(escape(money(group.get(field, 0), group['currency'])) for group in groups) or '—'


def _kpi(label, value, note, primary=False):
    return '<div class="expense-kpi' + (' primary' if primary else '') + '"><h2>' + escape(label) + '</h2><strong>' + value + '</strong><small>' + escape(note) + '</small></div>'


def render(entries, totals, *, year=None, report=None, years=None, archive_status=None, dashboard=None, profiles=None, business=None):
    """Render private data supplied by the caller; no instance identities live here."""
    year = year or datetime.now().year
    report = report or {'months': [], 'categories': [], 'monthly_categories': []}
    dashboard = dashboard or {}
    profiles = [dict(p, key=p.get('key') or p['business_key']) for p in (profiles or [])]
    years = sorted(set(years or [year]) | {year}, reverse=True)
    annotations = {item['id']: item.get('accounting') or {} for item in dashboard.get('entries', [])}
    entries = [dict(item, accounting=annotations.get(item['id'], item.get('accounting') or {})) for item in entries]
    active = [item for item in entries if item['status'] != 'excluded' and item.get('document_type') in ('receipt', 'refund') and (not business or item.get('business_key') == business)]
    if business:
        totals = [g for g in totals if g.get('business_key', business) == business]
    business_names = {p['key']: p.get('name') or p['key'] for p in profiles}
    totals = [dict(g, business_name=business_names.get(g.get('business_key'), g.get('business_key', ''))) for g in totals]
    report = {key: [dict(g, business_name=business_names.get(g.get('business_key'), g.get('business_key', ''))) for g in groups] if isinstance(groups, list) else groups for key, groups in report.items()}
    profile = dashboard.get('profile') or next((p for p in profiles if p['key'] == business), {})
    options = ''.join(f'<option value="{value}"{" selected" if value == year else ""}>{value}</option>' for value in years)
    business_options = ''.join('<option value="' + escape(str(p['key'])) + '"' + (' selected' if p['key'] == business else '') + '>' + escape(p.get('name') or p['key']) + '</option>' for p in profiles)
    body = _STYLES + '<div class="expense-head"><div><div class="expense-eyebrow">Business accounting</div><h1>' + escape(profile.get('name') or 'Business expenses') + '</h1></div>'
    body += '<form class="expense-filters" method="get" action="/expenses">' + ('<label>Business <select name="business" onchange="this.form.submit()">' + business_options + '</select></label>' if profiles else '')
    body += f'<label>Calendar year <select name="year" onchange="this.form.submit()">{options}</select></label><noscript><button>Show year</button></noscript></form></div>'
    if profile.get('trade_names'):
        body += '<p class="hint">Registered trade names: ' + escape(', '.join(profile['trade_names'])) + '</p>'
    body += '<p class="expense-subtitle">Your paid expenses, supporting receipts, and tax preparation notes in one place. Bills and payment reminders stay filed in email; they do not count here.</p>'
    ready = sum(1 for row in active if row.get('status') == 'ready' and not row.get('duplicate_of') and (row.get('accounting') or {}).get('transaction_role') not in ('financing_principal', 'transfer', 'non_business'))
    body += '<div class="expense-kpis">' + _kpi('Business-share payments', _currency_values(totals, 'receipts_minor'), 'Business-use percentages applied; includes equipment.')
    body += _kpi('Refunds and credits', _currency_values(totals, 'refunds_minor'), 'Completed refunds reduce the original cost.')
    body += _kpi('Net business spending', _currency_values(totals, 'net_paid_minor'), 'Allocated spending, not a final tax deduction.', True)
    body += _kpi('Receipts reviewed', f'{ready} <span style="font-size:.8em;color:var(--muted)">/ {len(active)}</span>', 'Duplicates and unconfirmed amounts stay out of totals.') + '</div>'
    query = urlencode(dict(year=year, **({'business': business} if business else {})))
    records_query = urlencode(dict(scope='all', purpose='records', **({'business': business} if business else {})))
    body += f'<nav class="expense-tools" aria-label="Accounting downloads"><a href="/expenses.csv?{escape(query)}">Download {year} accounting CSV</a><a href="/expenses.zip?{escape(query)}">Download {year} accounting ZIP</a><a href="/expenses.zip?{escape(records_query)}">Download all records (includes excluded items)</a></nav>'
    body += _preparation(dashboard, profile, active, year, business)
    body += '<details class="expense-panel"><summary>Receipt archive and downloads</summary><p>Accounting downloads include ready receipts and completed refunds for this business. Removed entries, possible duplicates, unpaid bills, and items awaiting review stay out. Cash spending and tax treatment are separate: equipment and capitalized costs must not also be deducted as ordinary expenses.</p><p>The all-records download is for recovery and includes excluded records. ZIP files retain the ledger, comments, categories, review history, and available original emails. Removing a ledger entry never deletes its email.</p>'
    if archive_status:
        body += '<p>Original emails archived: ' + str(archive_status['archived']) + ' of ' + str(archive_status['total']) + '. ' + ('All receipt originals are available.' if archive_status['complete'] else 'The ZIP manifest identifies originals still awaiting recovery.') + '</p>'
    body += '<p class="hint">Automatic recovery backups include the private accounting data. Configure an off-server destination using the recovery instructions.</p></details>'
    by_month = {}
    for item in active:
        raw_date = item.get('document_date') or item.get('received_at') or ''
        key = raw_date[:7] if len(raw_date) >= 7 else 'undated'
        by_month.setdefault(key, []).append(item)
    entry_details = {item['id']: dict(item.get('accounting') or {}, _currency=item.get('currency')) for item in dashboard.get('entries', [])}
    for month in range(1, 13):
        key = f'{year}-{month:02d}'
        month_entries = by_month.get(key, [])
        groups = [g for g in report.get('months', []) if g['month'] == key and (not business or g.get('business_key', business) == business)]
        categories = [g for g in report.get('monthly_categories', []) if g['month'] == key and (not business or g.get('business_key', business) == business)]
        if not month_entries and not groups:
            continue
        body += f'<section class="expense-month" id="month-{month:02d}"><h2>{calendar.month_name[month]} {year} <span class="expense-badge">{len(month_entries)} {"record" if len(month_entries) == 1 else "records"}</span></h2>' + _totals(groups)
        body += '<details class="expense-panel"><summary>Totals by category</summary>' + _totals(categories) + '</details>'
        body += ''.join(_card(item, year, business, profiles, entry_details.get(item['id'], entry_details.get(str(item['id']), {})) if dashboard else None) for item in month_entries)
        body += '</section>'
    if by_month.get('undated'):
        body += '<section><h2>Date needs review</h2>' + ''.join(_card(item, year, business, profiles, entry_details.get(item['id'], {}) if dashboard else None) for item in by_month['undated']) + '</section>'
    body += f'<section class="expense-panel" id="year-total"><h2>{year} total</h2>' + _totals(totals) + '<details><summary>Year totals by category</summary>' + _totals([g for g in report.get('categories', []) if not business or g.get('business_key', business) == business]) + '</details></section>'
    if not active:
        body += '<div class="expense-empty">No paid receipts or completed refunds for this year yet. Missing receipts are tracked separately below; filed invoices remain in your mailbox.</div>'
    body += _expected(dashboard, year, business)
    return body


def _input(label, name, value='', *, kind='text', extra=''):
    return '<label>' + escape(label) + ' <input type="' + kind + '" name="' + name + '" value="' + escape(str(value if value is not None else '')) + '" ' + extra + '></label>'


def _accounting_form(identifier, data, year, business):
    asset = data.get('asset') or {}
    role = data.get('transaction_role', 'expense')
    roles = {'expense': 'Business expense', 'asset': 'Equipment or intangible asset', 'refund': 'Refund / purchase reversal', 'financing_principal': 'Financing principal — not an expense', 'transfer': 'Transfer — not an expense', 'non_business': 'Not a business expense'}
    html = '<details class="expense-subpanel"><summary>Accounting, allocation, and tax treatment</summary><p class="hint">Keep the bookkeeping account separate from its tax treatment. These are preparation notes; a saved classification does not establish a deduction.</p>'
    html += f'<form method="post" action="/expenses/{identifier}/accounting">' + _hidden(year, business) + '<div class="expense-form-grid">'
    html += '<label>Transaction role <select name="transaction_role">' + ''.join('<option value="' + k + '"' + (' selected' if k == role else '') + '>' + escape(v) + '</option>' for k, v in roles.items()) + '</select></label>'
    html += _input('Business use (%)', 'business_use_percent', Decimal(data.get('business_use_bps', 10000)) / 100, kind='number', extra='min="0" max="100" step="0.01" required')
    html += _input('Proposed tax treatment', 'tax_treatment', data.get('tax_treatment', ''), extra='maxlength="2000" placeholder="Needs review"')
    html += _input('Tax form / schedule', 'tax_form', data.get('tax_form', ''), extra='maxlength="2000"')
    html += _input('Tax return description', 'tax_description', data.get('tax_description', ''), extra='maxlength="2000"')
    html += _input('Related purchase or refund entry ID', 'related_entry_id', data.get('related_entry_id', ''), kind='number', extra='min="1"')
    html += '<label class="expense-full">Evidence and tax notes <textarea name="notes" rows="3" maxlength="12000">' + escape(data.get('notes', '')) + '</textarea></label></div>'
    if data.get('evidence_status'):
        html += '<p class="hint">Evidence: ' + escape(data['evidence_status']) + '</p>'
    html += '<details class="expense-subpanel"><summary>Split across bookkeeping accounts</summary><p class="hint">Allocate the business share using consistent percentages totaling 100%. Leave every line blank for one account. Splitting never creates an additional charge.</p><div class="expense-allocations">'
    allocations = list(data.get('allocations') or [])
    for index, allocation in enumerate(allocations + [{}] * max(2, 4 - len(allocations))):
        html += '<fieldset><legend>Allocation ' + str(index + 1) + '</legend><div class="expense-allocation">'
        html += _input('Account', 'allocation_category', allocation.get('category', ''), extra='maxlength="200"')
        html += _input('Share (%)', 'allocation_percent', Decimal(allocation['bps']) / 100 if 'bps' in allocation else '', kind='number', extra='min="0.01" max="100" step="0.01"')
        html += _input('Tax treatment', 'allocation_tax_treatment', allocation.get('tax_treatment', ''), extra='maxlength="1000"')
        html += _input('Tax form', 'allocation_tax_form', allocation.get('tax_form', ''), extra='maxlength="1000"')
        html += _input('Tax description', 'allocation_tax_description', allocation.get('tax_description', ''), extra='maxlength="1000"')
        html += '</div></fieldset>'
    html += '</div></details>'
    html += '<details class="expense-subpanel"><summary>Equipment and asset record</summary><p class="hint">An equipment purchase is cash spending. Its depreciation or amortization is a separate calculation and must not also be claimed as an ordinary expense. Use the actual ready-for-business date.</p><div class="expense-form-grid">'
    html += _input('Asset name', 'asset_name', asset.get('name', ''), extra='maxlength="1000"')
    html += _input('Purchase date', 'asset_purchase_date', asset.get('purchase_date', ''), kind='date')
    html += _input('Placed in service', 'asset_placed_in_service_date', asset.get('placed_in_service_date', ''), kind='date')
    # Monetary asset basis uses the receipt currency; keep integer-minor storage out of the UI.
    import business_ledger
    currency = data.get('_currency')
    basis = asset.get('basis_minor')
    basis_text = format(Decimal(basis) / (10 ** business_ledger.UNITS[currency]), 'f') if basis is not None and currency in business_ledger.UNITS else ''
    html += _input('Asset cost basis' + (' (' + currency + ')' if currency else ''), 'asset_basis', basis_text, extra='inputmode="decimal"')
    html += _input('Serial number (optional)', 'asset_serial_number', asset.get('serial_number', ''), extra='maxlength="1000"')
    html += _input('Proposed depreciation / amortization method', 'asset_depreciation_method', asset.get('depreciation_method', ''), extra='maxlength="1000"')
    html += '</div>'
    if asset and not asset.get('placed_in_service_date'):
        html += '<p><strong>Service date needed.</strong> No first-year deduction has been finalized.</p>'
    if asset.get('proposed_depreciation_minor') is not None:
        html += '<p>Proposed depreciation: ' + escape(money(asset['proposed_depreciation_minor'], currency)) + '. Subject to eligibility and filing review.</p>'
    html += '</details><details class="expense-subpanel"><summary>Payment timing, prepaid credits, and amortization</summary><p class="hint">A payment or prepaid-credit purchase is not automatically a deduction for the same period. Preserve consumption, service, and amortization dates for filing review.</p><div class="expense-form-grid">'
    for label, name in [('Payment date', 'payment_date'), ('Service period starts', 'service_period_start'), ('Service period ends', 'service_period_end'), ('Amortization starts', 'amortization_start_date')]:
        html += _input(label, name, data.get(name, ''), kind='date')
    balance = data.get('prepaid_balance_minor')
    balance_text = format(Decimal(balance) / (10 ** business_ledger.UNITS[currency]), 'f') if balance is not None and currency in business_ledger.UNITS else ''
    html += _input('Unused prepaid balance' + (' (' + currency + ')' if currency else ''), 'prepaid_balance', balance_text, extra='inputmode="decimal"')
    html += _input('Amortization period (months)', 'amortization_months', data.get('amortization_months', ''), kind='number', extra='min="1" max="1200"')
    html += '</div></details><p><button type="submit">Save accounting details</button></p></form></details>'
    return html


def _preparation(dashboard, profile, active, year, business):
    if not dashboard:
        return ''
    profile = dashboard.get('profile') or profile
    expected = [r for r in dashboard.get('expected', []) if r.get('status') not in ('matched', 'cancelled')]
    needs_tax = sum(1 for entry in active if not (entry.get('accounting') or {}).get('tax_treatment'))
    assets = [entry for entry in active if (entry.get('accounting') or {}).get('transaction_role') == 'asset' or (entry.get('accounting') or {}).get('asset')]
    undated_assets = sum(1 for entry in assets if not (entry.get('accounting') or {}).get('asset', {}).get('placed_in_service_date'))
    body = '<div class="expense-detail-grid"><section class="expense-panel"><div class="expense-eyebrow">Prepare for filing</div><h2>Keep the evidence together</h2><ul class="expense-checklist">'
    body += '<li>' + str(len(expected)) + ' expected or missing receipt records</li><li>' + str(needs_tax) + ' records need a proposed tax classification</li><li>' + str(len(assets)) + ' asset records; ' + str(undated_assets) + ' need a service date</li>'
    body += '<li>Business commencement: ' + escape(profile.get('commencement_date') or 'date not yet recorded') + '</li></ul><p class="hint">Tax notes remain provisional. Confirm payment evidence, business use, eligibility, and the final forms for the filing year.</p></section>'
    body += '<details class="expense-panel"><summary>Tax preparation and allocation policy</summary><p class="expense-note">' + escape(dashboard.get('tax_notice') or 'Review proposed tax treatment before filing.') + '</p>'
    if profile.get('guidance'):
        body += '<h3>Filing notes</h3><p class="expense-note">' + escape(profile['guidance']) + '</p>'
    if profile.get('policies'):
        body += '<h3>Allocation policy</h3><p class="expense-note">' + escape(profile['policies']) + '</p>'
    body += '<details class="expense-subpanel"><summary>Edit business dates and private notes</summary><form method="post" action="/expenses/profile">' + _hidden(year, business)
    body += _input('Business commencement date', 'commencement_date', profile.get('commencement_date', ''), kind='date')
    body += '<label>Allocation policy <textarea name="policies" rows="5" maxlength="40000">' + escape(profile.get('policies', '')) + '</textarea></label>'
    body += '<label>Filing notes <textarea name="guidance" rows="6" maxlength="40000">' + escape(profile.get('guidance', '')) + '</textarea></label><button>Save private business notes</button></form></details></details></div>'
    buckets = dashboard.get('provisional_tax_buckets', [])
    body += '<details class="expense-panel"><summary>Allocated spending and proposed tax mapping</summary><p>' + escape(dashboard.get('tax_notice') or 'Allocated spending is not a calculated tax deduction.') + '</p>'
    if buckets:
        body += '<div style="overflow-x:auto"><table class="expense-totals"><thead><tr><th>Bookkeeping account</th><th>Allocated amount</th><th>Proposed treatment</th><th>Form / schedule</th></tr></thead><tbody>'
        for row in buckets:
            body += '<tr><th scope="row">' + escape(row['category']) + '</th><td>' + escape(money(row['amount_minor'], row['currency'])) + '</td><td>' + escape(row.get('tax_treatment') or 'Needs review') + '</td><td>' + escape(row.get('tax_form') or 'Needs review') + '</td></tr>'
        body += '</tbody></table></div>'
    else:
        body += '<p>Confirm receipt amounts and add classifications to build the preparation summary.</p>'
    return body + '</details>'


def _expected(dashboard, year, business):
    rows = [row for row in dashboard.get('expected', []) if row.get('status') != 'cancelled']
    if not dashboard:
        return ''
    body = '<section class="expense-panel"><h2>Expected expenses and missing receipts</h2><p>These are planning and reconciliation records, not additional expenses. Only a matched, verified paid receipt enters accounting totals.</p>'
    for row in rows:
        state = {'expected': 'Expected', 'evidence_missing': 'Receipt needed', 'matched': 'Receipt matched'}.get(row.get('status'), 'Needs review')
        amount = money(row.get('amount_minor'), row.get('currency')) if row.get('amount_minor') is not None else 'Amount not yet known'
        body += '<details class="expense-subpanel"><summary>' + escape(row.get('date') or str(year)) + ' · ' + escape(row.get('vendor') or 'Vendor to confirm') + ' · ' + escape(amount) + ' <span class="expense-badge">' + escape(state) + '</span></summary>'
        body += '<p>' + escape(row.get('purpose') or '') + '</p><p>' + escape(row.get('category') or '') + '</p><p class="expense-note">' + escape(row.get('notes') or '') + '</p>'
        if row.get('entry_id'):
            body += '<p><a href="#expense-' + str(int(row['entry_id'])) + '">Go to matched expense</a></p>'
        body += _expected_form(row, year, business) + '</details>'
    body += '<details class="expense-subpanel"><summary>Add expected expense or missing receipt</summary>' + _expected_form({}, year, business) + '</details>'
    return body + '</section>'


def _expected_form(row, year, business):
    import business_ledger
    currency = row.get('currency') or 'USD'
    minor = row.get('amount_minor')
    amount = format(Decimal(minor) / 10 ** business_ledger.UNITS[currency], 'f') if minor is not None and currency in business_ledger.UNITS else ''
    body = '<form method="post" action="/expenses/expected">' + _hidden(year, business)
    body += '<input type="hidden" name="record_key" value="' + escape(row.get('record_key') or '') + '"><div class="expense-form-grid">'
    body += _input('Vendor', 'vendor', row.get('vendor', ''), extra='maxlength="500" required')
    body += _input('Expected or payment date (if known)', 'date', row.get('date', ''), kind='date')
    body += _input('Amount (if known)', 'amount', amount, extra='inputmode="decimal"')
    body += '<label>Currency <select name="currency">' + ''.join('<option' + (' selected' if c == currency else '') + '>' + c + '</option>' for c in business_ledger.UNITS) + '</select></label>'
    body += _input('Bookkeeping account', 'category', row.get('category', ''), extra='maxlength="200"')
    body += _input('Business purpose', 'purpose', row.get('purpose', ''), extra='maxlength="4000"')
    body += _input('Matched receipt entry ID (optional)', 'entry_id', row.get('entry_id', ''), kind='number', extra='min="1"')
    body += '<label>Record status <select name="status">' + ''.join('<option value="' + key + '"' + (' selected' if row.get('status', 'expected') == key else '') + '>' + label + '</option>' for key, label in (('expected', 'Expected'), ('evidence_missing', 'Paid, receipt missing'), ('matched', 'Matched to receipt'), ('cancelled', 'Cancelled'))) + '</select></label>'
    body += '<label class="expense-full">Notes <textarea name="notes" rows="3" maxlength="12000">' + escape(row.get('notes', '')) + '</textarea></label></div><p class="hint">A linked receipt must belong to this business and match the amount and currency. This planning record never creates a charge.</p><button>Save expected record</button></form>'
    return body
