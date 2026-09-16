from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'decision-app'))
import generate_sieve
from sievelib.parser import Parser


class SieveTests(unittest.TestCase):
    def test_generated_script_parses(self):
        script = generate_sieve.build_sieve({'blocked.example'}, {'marketing.example'})
        parser = Parser()
        self.assertTrue(parser.parse(script), getattr(parser, "error", "Invalid Sieve"))
        self.assertIn('exists "List-Unsubscribe"', script)

    def test_custom_rules_preserved_and_managed_section_replaced(self):
        original = '# My rules\nrequire ["fileinto"];\nif true { fileinto "Archive"; stop; }\n'
        first = generate_sieve.merge_sieve(original, generate_sieve.build_sieve({'blocked.example'}, set()))
        second = generate_sieve.merge_sieve(first, generate_sieve.build_sieve(set(), set()))
        self.assertIn('fileinto "Archive"', second)
        self.assertNotIn('blocked.example', second)
        self.assertEqual(second.count(generate_sieve.BEGIN), 1)
        self.assertLess(first.index('require'), first.index(generate_sieve.BEGIN))
        self.assertLess(first.index('discard'), first.index('if true'))
        parser = Parser()
        self.assertTrue(parser.parse(first), getattr(parser, "error", "Invalid Sieve"))

    def test_domain_injection_rejected(self):
        with self.assertRaises(ValueError):
            generate_sieve.build_sieve({'bad.example"; discard;'}, set())

    def test_malformed_marker_is_not_overwritten(self):
        with self.assertRaises(ValueError):
            generate_sieve.merge_sieve(generate_sieve.BEGIN, 'new')
