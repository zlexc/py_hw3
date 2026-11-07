"""Контракты моделей и сохранение состояния при ошибках."""

import hashlib
import unittest

from tests.helpers import NOW, make_user
from valutatrade_hub.core.models import Portfolio, User, Wallet


class UserTests(unittest.TestCase):
    def test_password_change_and_public_information(self):
        user = make_user()
        self.assertTrue(user.verify_password("1234"))
        self.assertFalse(user.verify_password("wrong"))
        user.change_password("пароль")
        expected = hashlib.sha256("парольtest-salt".encode()).hexdigest()
        self.assertEqual(user.hashed_password, expected)
        self.assertTrue(user.verify_password("пароль"))
        self.assertFalse(user.verify_password("1234"))
        self.assertEqual(
            user.get_user_info(),
            {"user_id": 1, "username": "alice", "registration_date": NOW.isoformat()},
        )

    def test_invalid_changes_preserve_existing_fields(self):
        user = make_user()
        original = user.hashed_password
        for password in ("", "123", None, 1234):
            with self.subTest(password=password), self.assertRaises(ValueError):
                user.change_password(password)
        self.assertEqual(user.hashed_password, original)
        for name in ("", "  ", None, 1):
            with self.subTest(name=name), self.assertRaises(ValueError):
                user.username = name
        self.assertEqual(user.username, "alice")
        user.username = " Alice "
        self.assertEqual(user.username, "Alice")

    def test_identity_fields_are_read_only(self):
        user = make_user()
        for field in ("user_id", "salt", "hashed_password", "registration_date"):
            with self.subTest(field=field), self.assertRaises(AttributeError):
                setattr(user, field, "replacement")

    def test_invalid_constructor_values(self):
        for user_id in (True, 0, -1, "1"):
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                User(user_id, "alice", "hash", "salt", NOW)

    def test_non_ascii_stored_hash_fails_verification_without_crashing(self):
        user = User(1, "alice", "некорректный хеш", "salt", NOW)
        self.assertFalse(user.verify_password("1234"))


class WalletTests(unittest.TestCase):
    def test_deposit_withdraw_and_balance_info(self):
        wallet = Wallet(" btc ")
        wallet.deposit(0.05)
        wallet.withdraw(0.01)
        self.assertAlmostEqual(wallet.balance, 0.04)
        self.assertEqual(wallet.get_balance_info()["currency_code"], "BTC")
        wallet.withdraw(wallet.balance)
        self.assertEqual(wallet.balance, 0.0)

    def test_invalid_numbers_leave_balance_unchanged(self):
        wallet = Wallet("USD", 10)
        invalid = (True, False, "2", None, -1, float("nan"), float("inf"), 10**400)
        for amount in invalid:
            for operation in (wallet.deposit, wallet.withdraw):
                with self.subTest(amount=amount, operation=operation.__name__):
                    with self.assertRaises(ValueError):
                        operation(amount)
                    self.assertEqual(wallet.balance, 10)
            with self.assertRaises(ValueError):
                wallet.balance = amount
            self.assertEqual(wallet.balance, 10)
        for operation in (wallet.deposit, wallet.withdraw):
            with self.assertRaises(ValueError):
                operation(0)

    def test_insufficient_funds_and_overflow(self):
        wallet = Wallet("BTC", 0.04)
        with self.assertRaisesRegex(ValueError, "Недостаточно средств"):
            wallet.withdraw(0.05)
        self.assertEqual(wallet.balance, 0.04)
        wallet.balance = 1e308
        with self.assertRaises(ValueError):
            wallet.deposit(1e308)
        self.assertEqual(wallet.balance, 1e308)

    def test_invalid_currency_codes(self):
        for code in ("", "  ", None, "BTC_USD", "доллар", 123):
            with self.subTest(code=code), self.assertRaises(ValueError):
                Wallet(code)


class PortfolioTests(unittest.TestCase):
    def test_owner_and_wallet_dictionary_protection(self):
        user = make_user()
        original = {"USD": Wallet("USD", 10)}
        portfolio = Portfolio(1, user, original)
        original.clear()
        copy = portfolio.wallets
        copy.clear()
        self.assertEqual(portfolio.get_wallet("usd").balance, 10)
        self.assertIs(portfolio.user, user)
        with self.assertRaises(AttributeError):
            portfolio.user = make_user(2)
        with self.assertRaises(ValueError):
            Portfolio(2, user)

    def test_adding_duplicate_and_retrieving_missing_wallet(self):
        portfolio = Portfolio(1, make_user())
        wallet = portfolio.add_currency(" eur ")
        self.assertIs(wallet, portfolio.get_wallet("EUR"))
        with self.assertRaisesRegex(ValueError, "уже существует"):
            portfolio.add_currency("eur")
        with self.assertRaisesRegex(ValueError, "нет кошелька 'BTC'"):
            portfolio.get_wallet("BTC")

    def test_total_value_in_usd_and_eur(self):
        portfolio = Portfolio(
            1,
            make_user(),
            {"USD": Wallet("USD", 150), "BTC": Wallet("BTC", 0.05)},
        )
        rates = {"USD": 1, "BTC": 60000, "EUR": 1.2}
        self.assertEqual(portfolio.get_total_value(exchange_rates=rates), 3150)
        self.assertEqual(portfolio.get_total_value("eur", exchange_rates=rates), 2625)
        self.assertAlmostEqual(portfolio.get_total_value(), 150 + 0.05 * 59337.21)
        with self.assertRaisesRegex(ValueError, "Неизвестная базовая валюта"):
            portfolio.get_total_value("ABC")
        with self.assertRaisesRegex(ValueError, "Не удалось получить курс"):
            portfolio.get_total_value(exchange_rates={"USD": 1})

    def test_empty_portfolio_has_zero_value(self):
        self.assertEqual(Portfolio(1, make_user()).get_total_value(), 0)
