"""Сквозные сценарии регистрации, торговли и командного интерфейса."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import NOW
from valutatrade_hub.cli.interface import WalletApplication
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.storage import JsonStorage, StorageError
from valutatrade_hub.core.usecases import WalletService


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.storage = JsonStorage(self.directory)
        self.service = WalletService(self.storage, clock=lambda: NOW)

    def register_and_login(self, name="alice", password="1234"):
        self.service.register(name, password)
        self.service.login(name, password)

    def account_files(self):
        return {
            filename: (self.directory / filename).read_bytes()
            for filename in ("users.json", "portfolios.json")
        }


class RegistrationTests(ServiceFixture):
    def test_registration_is_persisted_but_does_not_login(self):
        result = self.service.register("alice", "a-secret-password")
        self.assertEqual(result["user_id"], 1)
        self.assertIsNone(self.service.current_user)
        users = self.storage.load_users()
        self.assertNotEqual(users[0].hashed_password, "a-secret-password")
        self.assertNotIn(
            "a-secret-password", (self.directory / "users.json").read_text()
        )
        self.assertTrue(users[0].verify_password("a-secret-password"))
        self.assertEqual(self.storage.load_portfolios(users)[1].wallets, {})

    def test_duplicate_short_password_and_empty_name_leave_files_unchanged(self):
        self.service.register("alice", "1234")
        original = self.account_files()
        cases = (
            ("alice", "5678", "уже занято"),
            ("bob", "123", "не короче 4"),
            ("  ", "1234", "не может быть пустым"),
        )
        for name, password, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                self.service.register(name, password)
            self.assertEqual(self.account_files(), original)

    def test_ids_are_monotonic_names_case_sensitive_and_salts_unique(self):
        self.service.register("alice", "1234")
        self.service.register("Alice", "1234")
        users = self.storage.load_users()
        self.assertEqual([user.user_id for user in users], [1, 2])
        self.assertNotEqual(users[0].salt, users[1].salt)
        self.assertNotEqual(users[0].hashed_password, users[1].hashed_password)

    def test_login_errors_and_failed_login_preserve_existing_session(self):
        self.register_and_login()
        original = self.service.current_user
        with self.assertRaisesRegex(ValueError, "Пользователь 'bob' не найден"):
            self.service.login("bob", "1234")
        with self.assertRaisesRegex(ValueError, "Неверный пароль"):
            self.service.login("alice", "bad")
        self.assertIs(self.service.current_user, original)

    def test_registration_rolls_back_user_if_portfolio_write_fails(self):
        original = self.account_files()
        with patch.object(
            self.storage, "save_portfolios", side_effect=StorageError("disk full")
        ):
            with self.assertRaises(StorageError):
                self.service.register("alice", "1234")
        self.assertEqual(self.account_files(), original)


class TradingTests(ServiceFixture):
    def test_session_required_but_get_rate_is_public(self):
        for operation in (
            lambda: self.service.show_portfolio(),
            lambda: self.service.buy("BTC", 0.05),
            lambda: self.service.sell("BTC", 0.01),
        ):
            with self.assertRaisesRegex(ValueError, "Сначала выполните login"):
                operation()
        result = self.service.get_rate("USD", "BTC")
        self.assertAlmostEqual(result["rate"], 1 / 59337.21)
        self.assertAlmostEqual(result["inverse_rate"], 59337.21)

    def test_empty_portfolio_and_unknown_base(self):
        self.register_and_login()
        result = self.service.show_portfolio()
        self.assertEqual(result["wallets"], [])
        self.assertEqual(result["total"], 0)
        with self.assertRaisesRegex(ValueError, "Неизвестная валюта 'ABC'"):
            self.service.show_portfolio("abc")

    def test_buy_sell_and_restart_preserve_balances_without_usd_settlement(self):
        self.register_and_login()
        self.service.buy("USD", 150)
        bought = self.service.buy("btc", 0.05)
        self.assertEqual((bought["before"], bought["after"]), (0, 0.05))
        self.assertAlmostEqual(bought["estimated_value"], 0.05 * 59337.21)
        sold = self.service.sell("BTC", 0.01)
        self.assertAlmostEqual(sold["after"], 0.04)
        restarted = WalletService(JsonStorage(self.directory), clock=lambda: NOW)
        self.assertIsNone(restarted.current_user)
        restarted.login("alice", "1234")
        result = restarted.show_portfolio()
        balances = {
            wallet["currency_code"]: wallet["balance"] for wallet in result["wallets"]
        }
        self.assertEqual(balances["USD"], 150)
        self.assertAlmostEqual(balances["BTC"], 0.04)
        self.assertAlmostEqual(result["total"], 150 + 0.04 * 59337.21)
        self.assertAlmostEqual(
            restarted.show_portfolio("eur")["total"], result["total"] / 1.0786
        )

    def test_new_portfolio_buy_does_not_create_usd_wallet(self):
        self.register_and_login()
        self.service.buy("EUR", 200)
        result = self.service.show_portfolio()
        self.assertEqual(
            [wallet["currency_code"] for wallet in result["wallets"]], ["EUR"]
        )

    def test_rejected_trades_do_not_change_files(self):
        self.register_and_login()
        self.service.buy("BTC", 0.04)
        original = self.account_files()
        operations = [
            lambda: self.service.sell("BTC", 0.05),
            lambda: self.service.sell("ETH", 1),
            lambda: self.service.buy("ABC", 1),
            lambda: self.service.buy("BTC", 1e308),
        ]
        for amount in (0, -1, True, "2", float("nan"), float("inf")):
            operations.extend(
                [
                    lambda amount=amount: self.service.buy("BTC", amount),
                    lambda amount=amount: self.service.sell("BTC", amount),
                ]
            )
        for operation in operations:
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.account_files(), original)

    def test_rate_failure_and_write_failure_leave_portfolio_unchanged(self):
        self.register_and_login()
        self.service.buy("BTC", 0.04)
        original = self.account_files()
        failed_rates = RateService(
            self.storage,
            provider=Mock(side_effect=OSError("offline")),
            clock=lambda: NOW.replace(hour=13),
        )
        self.service.rates = failed_rates
        for operation in (self.service.buy, self.service.sell):
            with self.assertRaisesRegex(ApiRequestError, "Курс BTC→USD недоступен"):
                operation("BTC", 0.01)
            self.assertEqual(self.account_files(), original)
        self.service.rates = RateService(self.storage, clock=lambda: NOW)
        with patch.object(
            self.storage, "save_portfolios", side_effect=StorageError("disk full")
        ):
            with self.assertRaises(StorageError):
                self.service.buy("ETH", 1)
        self.assertEqual(self.account_files(), original)

    def test_multiple_users_cannot_modify_each_others_portfolios(self):
        self.register_and_login()
        self.service.buy("BTC", 0.05)
        self.service.register("bob", "5678")
        self.service.login("bob", "5678")
        self.assertEqual(self.service.show_portfolio()["wallets"], [])
        self.service.buy("EUR", 20)
        self.service.login("alice", "1234")
        result = self.service.show_portfolio()
        self.assertEqual(len(result["wallets"]), 1)
        self.assertEqual(result["wallets"][0]["currency_code"], "BTC")
        self.assertEqual(result["wallets"][0]["balance"], 0.05)


class CliTests(ServiceFixture):
    def run_session(self, commands):
        lines = iter([*commands, "exit"])
        output = io.StringIO()
        app = WalletApplication(
            self.service, input_fn=lambda: next(lines), output=output
        )
        app.run()
        return output.getvalue()

    def test_complete_session_with_quoted_arguments(self):
        output = self.run_session(
            [
                'register --username "Alice Smith" --password "my secret"',
                'login --username "Alice Smith" --password "my secret"',
                "show-portfolio",
                "buy --currency btc --amount 0.05",
                "sell --currency BTC --amount 0.01",
                "show-portfolio --base EUR",
                "get-rate --from USD --to BTC",
            ]
        )
        for text in (
            "зарегистрирован (id=1)",
            "Вы вошли как 'Alice Smith'",
            "Портфель пуст",
            "Покупка выполнена",
            "Продажа выполнена",
            "Стоимость, EUR",
            "ИТОГО:",
            "Курс USD→BTC:",
            "Обратный курс BTC→USD:",
            "До свидания!",
        ):
            self.assertIn(text, output)
        self.assertNotIn("Ошибка:", output)
        user = self.storage.load_users()[0]
        for secret in ("my secret", user.hashed_password, user.salt):
            self.assertNotIn(secret, output)

    def test_malformed_commands_do_not_end_session_or_echo_passwords(self):
        output = self.run_session(
            [
                " ",
                'register --username "broken',
                "login --password top-secret --unknown top-secret",
                "unknown-command",
                "register --username alice --password 1234",
                "login --username alice --password 1234",
                "buy --currency BTC --amount not-a-number",
                "buy --currency BTC --amount nan",
                "buy --currency BTC --amount inf",
                "buy --currency BTC --amount -1",
                "buy --currency BTC --amount 0.05",
                "sell --currency BTC --amount 1",
                "help",
                "buy --help",
                "show-portfolio",
            ]
        )
        self.assertIn("проверьте кавычки", output)
        self.assertIn("Некорректные аргументы", output)
        self.assertIn("'amount' должен быть положительным числом", output)
        self.assertIn("Недостаточно средств", output)
        self.assertIn("Покупка выполнена", output)
        self.assertIn("ИТОГО:", output)
        self.assertNotIn("top-secret", output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(output.rstrip().endswith("До свидания!"))

    def test_eof_and_keyboard_interrupt_exit_cleanly(self):
        for exception in (EOFError, KeyboardInterrupt):
            output = io.StringIO()
            with self.subTest(exception=exception):
                WalletApplication(
                    self.service, input_fn=Mock(side_effect=exception), output=output
                ).run()
                self.assertIn("До свидания!", output.getvalue())

    def test_corrupt_file_is_reported_and_preserved_during_session(self):
        target = self.directory / "portfolios.json"
        target.write_text("broken JSON")
        output = self.run_session(["register --username alice --password 1234"])
        self.assertIn("Ошибка:", output)
        self.assertEqual(target.read_text(), "broken JSON")
        self.assertEqual(json.loads((self.directory / "users.json").read_text()), [])

    def test_typed_errors_offer_specific_guidance_and_session_continues(self):
        self.service.rates = RateService(
            self.storage,
            provider=Mock(side_effect=OSError("offline")),
            clock=lambda: NOW,
        )
        output = self.run_session(
            [
                "register --username alice --password 1234",
                "login --username alice --password 1234",
                "sell --currency BTC --amount 1",
                "buy --currency ABC --amount 1",
                "get-rate --from ABC --to USD",
                "get-rate --from BTC --to USD",
                "help",
            ]
        )
        self.assertIn("Недостаточно средств: доступно 0.0000 BTC", output)
        self.assertIn("Неизвестная валюта 'ABC'", output)
        self.assertIn("Поддерживаемые валюты:", output)
        self.assertIn("get-rate --help", output)
        self.assertIn("Ошибка при обращении к внешнему API:", output)
        self.assertIn("Повторите попытку позже", output)
        self.assertIn("[FIAT] USD — US Dollar", output)
        self.assertIn("[CRYPTO] BTC — Bitcoin", output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(output.rstrip().endswith("До свидания!"))
