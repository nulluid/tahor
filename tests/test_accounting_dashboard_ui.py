"""Scoped dashboard presentation preserves business identity and evidence boundaries."""
import unittest
import expense_ui


def receipt(identifier=1, business='sample-studio', **updates):
    return dict(dict(id=identifier, business_key=business, document_type='receipt', status='ready',
                     document_date='2026-08-10', received_at='2026-08-10T12:00:00Z', date_basis='document',
                     currency='USD', amount_minor=10000, vendor='Example vendor', category='Tools',
                     owner_confirmed=1, metadata_confirmed=1, reference='R-1', review_reasons=[],
                     duplicate_of=None, comment='Business tools'), **updates)


class DashboardPresentationTests(unittest.TestCase):
    def test_scope_filters_bills_and_other_business_and_preserves_links(self):
        rows = [receipt(), receipt(2, document_type='invoice', vendor='Unpaid invoice vendor'),
                receipt(3, business='other-studio', vendor='Other business vendor'),
                receipt(4, status='excluded', vendor='Excluded vendor')]
        body = expense_ui.render(rows, [], year=2026, business='sample-studio',
                                 profiles=[{'business_key': 'sample-studio', 'name': 'Example Studio'}, {'business_key': 'other-studio', 'name': 'Other Studio'}],
                                 dashboard={'entries': [dict(rows[0], accounting={})]})
        self.assertIn('Example vendor', body)
        for name in ('Unpaid invoice vendor', 'Other business vendor', 'Excluded vendor'):
            self.assertNotIn(name, body)
        self.assertIn('/expenses/message/1?business=sample-studio', body)
        self.assertIn('name="business" value="sample-studio"', body)
        self.assertIn('year=2026&amp;business=sample-studio', body)
        self.assertIn('name="target_business"', body)
        self.assertNotIn('class="card expense-row" open', body)

    def test_allocations_assets_dates_and_private_text_are_safe(self):
        accounting = dict(tax_treatment='Review <eligibility>', business_use_bps=8000,
                          allocations=[dict(category='Development', bps=8000), dict(category='Administration', bps=2000)],
                          asset=dict(name='Work computer', basis_minor=300001, placed_in_service_date=''),
                          prepaid_balance_minor=750, payment_date='2026-08-10')
        row = receipt(accounting=accounting)
        dashboard = dict(profile=dict(name='Example', policies='<script>policy</script>', guidance='Private notes'),
                         entries=[row], expected=[], provisional_tax_buckets=[])
        body = expense_ui.render([row], [], year=2026, business='sample-studio', dashboard=dashboard)
        self.assertIn('value="3000.01"', body)
        self.assertIn('value="7.5"', body)
        self.assertIn('name="allocation_percent" value="80"', body)
        self.assertIn('Service date needed', body)
        self.assertIn('name="payment_date" value="2026-08-10"', body)
        self.assertIn('&lt;script&gt;policy&lt;/script&gt;', body)
        self.assertNotIn('<script>policy</script>', body)
        self.assertIn('Allocated spending, not a final tax deduction.', body)

    def test_expected_records_do_not_become_cash_or_receipt_rows(self):
        expected = dict(vendor='Expected vendor', amount_minor=99900, currency='USD', status='evidence_missing',
                        record_key='example-expected', date='2026-08-10', notes='Need payment evidence')
        body = expense_ui.render([], [], year=2026, business='sample-studio', dashboard={'entries': [], 'expected': [expected]})
        self.assertIn('Receipt needed', body)
        self.assertIn('Expected vendor', body)
        self.assertIn('/expenses/expected', body)
        self.assertIn('name="record_key" value="example-expected"', body)
        self.assertNotIn('class="card expense-row"', body)
        self.assertIn('planning and reconciliation records, not additional expenses', body)
        self.assertIn('Add expected expense or missing receipt', body)


if __name__ == '__main__':
    unittest.main()
