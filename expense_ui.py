"""Private expense review and export, separated from mailbox actions."""
from html import escape
from decimal import Decimal


def money(value, currency):
    import business_ledger
    if value is None or currency not in business_ledger.UNITS:
        return 'Needs review'
    return currency + ' ' + format(Decimal(value) / (10 ** business_ledger.UNITS[currency]), '.' + str(business_ledger.UNITS[currency]) + 'f')


def render(entries, totals):
    import business_ledger
    summary = ''.join('<div class="card"><strong>' + escape(group['business_key']) + '</strong><p>Paid receipts less refunds: ' + escape(money(group['net_paid_minor'], group['currency'])) + '</p><p>Invoices, tracked separately: ' + escape(money(group['invoices_minor'], group['currency'])) + '</p></div>' for group in totals)
    cards = []
    for item in entries[:500]:
        details = '<p>' + escape(' · '.join(item['review_reasons'])) + '</p>' if item['review_reasons'] else ''
        if item['duplicate_of']:
            details += '<p>Possible duplicate of entry ' + str(int(item['duplicate_of'])) + '; excluded from totals.</p>'
        kinds = ''.join(f'<option value="{kind}"{" selected" if kind == item["document_type"] else ""}>{kind.title()}</option>' for kind in business_ledger.KINDS)
        currencies = ''.join(f'<option value="{currency}"{" selected" if currency == item["currency"] else ""}>{currency}</option>' for currency in business_ledger.UNITS)
        amount = '' if item['amount_minor'] is None or item['currency'] not in business_ledger.UNITS else money(abs(item['amount_minor']),item['currency']).split(' ',1)[1]
        details += (f'<details><summary>Review or correct ledger values</summary><form method="post" action="/expenses/{item["id"]}/confirm">'
                    f'<label>Document type <select name="document_type">{kinds}</select></label>'
                    f'<label>Currency <select name="currency"><option value="">Choose currency</option>{currencies}</select></label>'
                    f'<label>Amount <input type="text" name="amount" value="{escape(amount)}" inputmode="decimal" required></label>'
                    f'<label>Document date <input type="date" name="document_date" value="{escape(item["document_date"] or "")}" required></label>'
                    f'<label>Receipt or invoice reference <input type="text" name="reference" maxlength="80" value="{escape(item["reference"] or "")}"></label>'
                    + (('<label><input type="checkbox" name="distinct_document" value="1" required> I verified this is a separate transaction, not a duplicate.</label>' if item['duplicate_of'] else '') + '<button type="submit">Confirm ledger values</button></form></details>'))
        details += f'<form method="post" action="/expenses/{item["id"]}/exclude"><button type="submit">Exclude from ledger totals</button></form>'
        cards.append(f'<div class="card"><h2>{escape(item["vendor"])}</h2><p>{escape(item["document_date"] or "Date needs review")}{" (email date)" if item["date_basis"] == "email_date" else ""} · {escape(item["document_type"])} · {escape(item["status"].replace("_"," "))}</p><p>{escape(money(item["amount_minor"],item["currency"]))}</p><p><a href="/expenses/message/{item["id"]}">View receipt email</a></p>{details}</div>')
    return ('<h1>Business expenses</h1><p>Receipt evidence from your private business filing rules. Amounts needing review and possible duplicates are excluded from totals. Invoices are shown separately from paid receipts; currencies are never combined.</p><p><a href="/expenses.csv">Download CSV ledger</a></p>' + summary + (''.join(cards) if cards else '<p>No business receipt evidence has been recorded yet. The business filing scan will add matching messages.</p>') + ('<p>Showing the newest 500 entries. The CSV export includes all entries.</p>' if len(entries)>500 else ''))
