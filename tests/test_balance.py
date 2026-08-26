import unittest

from farm import _extract_gram_balance


class BalanceParsingTests(unittest.TestCase):
    def test_plain_balance(self):
        self.assertEqual(_extract_gram_balance("💰 Баланс: 7500 GRAM"), 7500)

    def test_spaced_balance(self):
        self.assertEqual(_extract_gram_balance("Баланс 7 500 грам"), 7500)

    def test_decimal_does_not_inflate(self):
        self.assertEqual(_extract_gram_balance("Баланс: 7500.50 GRAM"), 7500)

    def test_unrelated_number_is_ignored(self):
        self.assertIsNone(_extract_gram_balance("До бонуса 7500 секунд"))
