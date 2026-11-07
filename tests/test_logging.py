"""Поля журналов, ротация и прозрачность декоратора."""

import inspect
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import NOW, make_user
from valutatrade_hub.cli.interface import WalletApplication
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
)
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.storage import JsonStorage, StorageError
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.decorators import log_action
from valutatrade_hub.logging_config import (
    ACTION_LOGGER_NAME,
    JsonActionFormatter,
    setup_logging,
)


class ActionLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.logger = logging.getLogger(ACTION_LOGGER_NAME)
        self.previous = (
            self.logger.handlers[:],
            self.logger.level,
            self.logger.propagate,
        )
        self.logger.handlers = []
        self.addCleanup(self.restore_logger)
        self.settings = {"LOG_DIR": self.directory / "logs"}
        setup_logging(self.settings)
        self.logfile = self.directory / "logs/actions.log"
        self.storage = JsonStorage(self.directory / "data")
        self.service = WalletService(self.storage, clock=lambda: NOW)

    def restore_logger(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()
        handlers, level, propagate = self.previous
        self.logger.handlers = handlers
        self.logger.setLevel(level)
        self.logger.propagate = propagate

    def records(self):
        def reject_nonfinite(value):
            raise AssertionError(f"Некорректная JSON-константа: {value}")

        return [
            json.loads(line, parse_constant=reject_nonfinite)
            for line in self.logfile.read_text(encoding="utf-8").splitlines()
        ]

    def login(self):
        self.service.register("alice", "a-secret-password")
        self.service.login("alice", "a-secret-password")

    def test_success_and_failure_records_have_expected_fields_and_no_credentials(self):
        self.login()
        self.assertEqual(self.records(), [])  # register/login не декорированы
        self.service.buy("btc", 0.05)
        self.service.sell("BTC", 0.01)
        with self.assertRaises(InsufficientFundsError) as failure:
            self.service.sell("BTC", 1)
        records = self.records()
        self.assertEqual(
            [record["action"] for record in records], ["BUY", "SELL", "SELL"]
        )
        self.assertEqual(
            [record["result"] for record in records], ["OK", "OK", "ERROR"]
        )
        bought, sold, failed = records
        for record in records:
            self.assertEqual(record["level"], "INFO")
            self.assertTrue(record["timestamp"].endswith("+00:00"))
            self.assertEqual(record["username"], "alice")
            self.assertEqual(record["user_id"], 1)
            self.assertEqual(record["currency_code"], "BTC")
            self.assertEqual(record["base"], "USD")
        self.assertEqual(bought["rate"], 59337.21)
        self.assertEqual((bought["before"], bought["after"]), (0, 0.05))
        self.assertAlmostEqual(sold["after"], 0.04)
        self.assertIsNone(bought["error_type"])
        self.assertEqual(failed["error_type"], "InsufficientFundsError")
        self.assertEqual(failed["error_message"], str(failure.exception))
        self.assertIsNone(failed["rate"])
        self.assertNotIn("after", failed)
        raw = self.logfile.read_text(encoding="utf-8")
        user = self.storage.load_users()[0]
        for secret in ("a-secret-password", user.hashed_password, user.salt):
            self.assertNotIn(secret, raw)

    def test_invalid_inputs_and_missing_session_still_produce_valid_json(self):
        with self.assertRaisesRegex(ValueError, "Сначала выполните login"):
            self.service.buy("BTC", 1)
        self.login()
        with self.assertRaises(CurrencyNotFoundError):
            self.service.buy("ABC", 1)
        for amount in (float("nan"), float("inf"), True, "a-secret-password", -1):
            with self.assertRaises(ValueError):
                self.service.buy("BTC", amount)
        records = self.records()
        self.assertEqual(len(records), 7)
        self.assertIsNone(records[0]["username"])
        self.assertEqual(records[1]["currency_code"], "ABC")
        self.assertTrue(all(record["result"] == "ERROR" for record in records))
        for record in records[2:6]:
            self.assertIsNone(record["amount"])
        self.assertEqual(records[-1]["amount"], -1)
        self.assertNotIn("a-secret-password", self.logfile.read_text())

    def test_cli_invalid_amount_is_logged_without_echoing_raw_input(self):
        self.login()
        app = WalletApplication(self.service, output=io.StringIO())
        with self.assertRaisesRegex(ValueError, "положительным числом"):
            app.handle_line("buy --currency BTC --amount a-secret-password")
        record = self.records()[0]
        self.assertEqual(record["result"], "ERROR")
        self.assertIsNone(record["amount"])
        self.assertNotIn("a-secret-password", self.logfile.read_text())

    def test_provider_and_storage_errors_are_logged_without_success(self):
        self.login()
        provider = Mock(side_effect=OSError("offline"))
        self.service.rates = RateService(
            self.storage, provider=provider, clock=lambda: NOW
        )
        original = (self.storage.data_dir / "portfolios.json").read_bytes()
        with self.assertRaises(ApiRequestError):
            self.service.buy("BTC", 0.05)
        self.service.rates = RateService(self.storage, clock=lambda: NOW)
        error = StorageError("disk full")
        with patch.object(self.storage, "save_portfolios", side_effect=error):
            with self.assertRaises(StorageError) as failure:
                self.service.buy("BTC", 0.05)
        self.assertIs(failure.exception, error)
        self.assertEqual(
            (self.storage.data_dir / "portfolios.json").read_bytes(), original
        )
        records = self.records()
        self.assertEqual([record["result"] for record in records], ["ERROR", "ERROR"])
        self.assertEqual(records[0]["error_type"], "ApiRequestError")
        self.assertEqual(records[1]["error_type"], "StorageError")

    def test_decorator_preserves_signature_and_optional_verbose_context(self):
        class Example:
            current_user = make_user()

            @log_action("BUY")
            def buy(self, currency_code, amount):
                """Документация метода."""
                return {"rate": 2, "before": 1, "after": 2}

        method = Example().buy
        self.assertEqual(method.__name__, "buy")
        self.assertEqual(method.__doc__, "Документация метода.")
        self.assertEqual(
            list(inspect.signature(method).parameters), ["currency_code", "amount"]
        )
        result = method(currency_code="EUR", amount=1)
        self.assertEqual(result, {"rate": 2, "before": 1, "after": 2})
        record = self.records()[0]
        self.assertNotIn("before", record)
        self.assertNotIn("after", record)

    def test_invalid_method_call_is_logged_and_original_type_error_propagates(self):
        with self.assertRaises(TypeError):
            self.service.buy("BTC", 1, unexpected=True)
        record = self.records()[0]
        self.assertEqual(record["error_type"], "TypeError")
        self.assertEqual(record["result"], "ERROR")

    def test_formatter_does_not_serialize_raw_record_or_unapproved_fields(self):
        record = logging.LogRecord(
            ACTION_LOGGER_NAME,
            logging.INFO,
            __file__,
            1,
            "password=%s",
            ("raw-secret",),
            None,
        )
        record.action_data = {
            "action": "BUY",
            "result": "OK",
            "password": "extra-secret",
        }
        formatted = JsonActionFormatter().format(record)
        self.assertNotIn("raw-secret", formatted)
        self.assertNotIn("extra-secret", formatted)
        self.assertNotIn("password", formatted)

    def test_logging_failure_does_not_change_committed_result_or_business_error(self):
        self.login()
        with patch.object(self.logger, "info", side_effect=OSError("log unavailable")):
            result = self.service.buy("BTC", 0.05)
            self.assertEqual(result["after"], 0.05)
            with self.assertRaises(InsufficientFundsError):
                self.service.sell("BTC", 1)
        portfolio = self.storage.load_portfolios(self.storage.load_users())[1]
        self.assertEqual(portfolio.get_wallet("BTC").balance, 0.05)

    def test_setup_is_idempotent_and_rotation_keeps_only_configured_backups(self):
        self.settings.update(LOG_MAX_BYTES=1000, LOG_BACKUP_COUNT=2)
        first = setup_logging(self.settings)
        handler = first.handlers[0]
        second = setup_logging(self.settings)
        self.assertIs(first, second)
        self.assertIs(second.handlers[0], handler)
        self.assertEqual(len(second.handlers), 1)
        self.assertFalse(second.propagate)
        self.login()
        self.service.buy("BTC", 0.001)
        self.assertEqual(len(self.records()), 1)
        for _ in range(15):
            self.service.buy("BTC", 0.001)
        files = list(self.logfile.parent.glob("actions.log*"))
        self.assertEqual(
            {file.name for file in files},
            {"actions.log", "actions.log.1", "actions.log.2"},
        )
        for file in files:
            for line in file.read_text(encoding="utf-8").splitlines():
                self.assertEqual(json.loads(line)["result"], "OK")
        setup_logging({**self.settings, "LOG_LEVEL": "DEBUG"})
        self.assertEqual(self.logger.level, logging.DEBUG)
        self.assertIs(self.logger.handlers[0], handler)
