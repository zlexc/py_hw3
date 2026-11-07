"""Наследование, инварианты, фабрика валют и типы ошибок."""

import unittest

from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    Currency,
    FiatCurrency,
    get_currency,
    supported_codes,
)
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    DomainError,
    InsufficientFundsError,
)
from valutatrade_hub.core.models import Wallet


class CurrencyTests(unittest.TestCase):
    def test_base_is_abstract_and_displays_are_polymorphic(self):
        with self.assertRaises(TypeError):
            Currency("US Dollar", "USD")
        currencies = [
            FiatCurrency("US Dollar", "USD", "United States"),
            CryptoCurrency("Bitcoin", "BTC", "SHA-256", 1.12e12),
        ]
        self.assertEqual(
            [currency.get_display_info() for currency in currencies],
            [
                "[FIAT] USD — US Dollar (Issuing: United States)",
                "[CRYPTO] BTC — Bitcoin (Algo: SHA-256, MCAP: 1.12e12)",
            ],
        )

    def test_codes_enforce_uppercase_length_and_no_spaces(self):
        for code in ("U", "ABCDEF", "usd", " USD", "US D", "US_D", 123, ""):
            with self.subTest(code=code), self.assertRaises(ValueError):
                FiatCurrency("Dollar", code, "United States")
        for code in ("AB", "ABC12"):
            with self.subTest(code=code):
                self.assertEqual(FiatCurrency("Test", code, "Test").code, code)

    def test_assignment_validation_preserves_previous_values(self):
        fiat = get_currency("USD")
        for field, value in (("name", " "), ("code", "usd"), ("issuing_country", None)):
            original = getattr(fiat, field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                setattr(fiat, field, value)
            self.assertEqual(getattr(fiat, field), original)
        crypto = get_currency("BTC")
        with self.assertRaises(ValueError):
            crypto.algorithm = ""
        for value in (True, -1, "10", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                crypto.market_cap = value
            self.assertEqual(crypto.market_cap, 1.12e12)
        crypto.market_cap = 0
        self.assertIn("MCAP: 0.00e0", crypto.get_display_info())

    def test_factory_normalizes_and_creates_independent_instances(self):
        first = get_currency(" btc ")
        second = get_currency("BTC")
        first.name = "Changed"
        self.assertIsNot(first, second)
        self.assertEqual(second.name, "Bitcoin")
        self.assertEqual(set(supported_codes()), {"USD", "EUR", "RUB", "BTC", "ETH"})
        self.assertIsInstance(get_currency("USD"), FiatCurrency)
        self.assertIsInstance(get_currency("ETH"), CryptoCurrency)
        with self.assertRaisesRegex(CurrencyNotFoundError, "Неизвестная валюта 'ABC'"):
            get_currency("abc")

    def test_empty_names_and_additional_text_fields_are_invalid(self):
        for value in ("", "  ", None, 123):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    FiatCurrency(value, "USD", "United States")
                with self.assertRaises(ValueError):
                    FiatCurrency("Dollar", "USD", value)
                with self.assertRaises(ValueError):
                    CryptoCurrency("Bitcoin", "BTC", value, 1)


class ExceptionTests(unittest.TestCase):
    def test_exception_messages_and_payloads(self):
        error = InsufficientFundsError(0.04, 0.05, "BTC")
        self.assertEqual(
            str(error),
            "Недостаточно средств: доступно 0.0400 BTC, требуется 0.0500 BTC",
        )
        self.assertEqual(
            (error.available, error.required, error.code), (0.04, 0.05, "BTC")
        )
        for error in (error, CurrencyNotFoundError("ABC"), ApiRequestError("offline")):
            self.assertIsInstance(error, DomainError)
            self.assertIsInstance(error, ValueError)
        self.assertEqual(
            str(ApiRequestError("offline")),
            "Ошибка при обращении к внешнему API: offline",
        )

    def test_wallet_raises_typed_error_and_preserves_balance(self):
        wallet = Wallet("BTC", 0.04)
        with self.assertRaises(InsufficientFundsError):
            wallet.withdraw(0.05)
        self.assertEqual(wallet.balance, 0.04)
