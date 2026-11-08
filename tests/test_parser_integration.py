"""Core/CLI/планировщик с настоящими клиентами и подменённым HTTP."""

import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from tests.helpers import NOW, make_user
from tests.test_parser_clients import coin_payload, fiat_payload, response
from valutatrade_hub.cli.interface import WalletApplication
from valutatrade_hub.core.exceptions import ApiRequestError, CurrencyNotFoundError
from valutatrade_hub.core.models import Portfolio, Wallet
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.storage import JsonStorage, StorageError
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.logging_config import PARSER_LOGGER_NAME, setup_parser_logging
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.scheduler import main, run_scheduler
from valutatrade_hub.parser_service.updater import UpdateResult, build_updater


class ParserIntegrationFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.settings = {
            "DATA_DIR": self.directory / "data",
            "LOG_DIR": self.directory / "logs",
        }
        self.config = ParserConfig(
            data_dir=self.settings["DATA_DIR"], exchangerate_api_key="fixture-key"
        )
        self.logger = logging.getLogger(PARSER_LOGGER_NAME)
        self.previous = (
            self.logger.handlers[:],
            self.logger.level,
            self.logger.propagate,
        )
        self.logger.handlers = []
        self.addCleanup(self.restore_logger)
        setup_parser_logging(self.settings)
        self.http = Mock()
        self.http.get.side_effect = self.reply
        self.storage = JsonStorage(self.config.data_dir)
        self.updater = build_updater(self.config, session=self.http, clock=lambda: NOW)
        self.rates = RateService(
            self.storage, refresher=self.updater.refresh, clock=lambda: NOW
        )
        self.service = WalletService(
            self.storage,
            rates=self.rates,
            updater=self.updater,
            clock=lambda: NOW,
            settings=self.settings,
        )

    def restore_logger(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()
        handlers, level, propagate = self.previous
        self.logger.handlers = handlers
        self.logger.setLevel(level)
        self.logger.propagate = propagate

    def reply(self, url, **kwargs):
        return response(coin_payload() if "coingecko" in url else fiat_payload())


class CoreParserTests(ParserIntegrationFixture):
    def test_portfolio_in_its_own_currency_needs_no_quotes_or_refresh(self):
        user = make_user()
        self.storage.save_users([user])
        self.storage.save_portfolios(
            {1: Portfolio(1, user, {"BTC": Wallet("BTC", 0.05)})}
        )
        self.service.login("alice", "1234")
        result = self.service.show_portfolio("BTC")
        self.assertEqual(result["total"], 0.05)
        self.assertEqual(result["wallets"][0]["value"], 0.05)
        self.http.get.assert_not_called()

    def test_portfolio_uses_direct_or_inverse_base_quotes_without_usd(self):
        user = make_user()
        self.storage.save_users([user])
        self.storage.save_portfolios(
            {1: Portfolio(1, user, {"BTC": Wallet("BTC", 0.05)})}
        )
        self.service.login("alice", "1234")
        for pair, rate in (("BTC_EUR", 50000), ("EUR_BTC", 1 / 50000)):
            with self.subTest(pair=pair):
                self.storage.save_rates(
                    {
                        pair: {
                            "rate": rate,
                            "updated_at": NOW.isoformat(),
                            "source": "Regression",
                        }
                    }
                )
                result = self.service.show_portfolio("EUR")
                self.assertEqual(result["total"], 2500)
                self.assertEqual(result["wallets"][0]["value"], 2500)
        self.http.get.assert_not_called()

    def test_auto_refresh_requests_only_needed_sources_and_fresh_cache_avoids_requests(
        self,
    ):
        self.assertEqual(self.service.get_rate("BTC", "USD")["rate"], 60000)
        self.http.get.assert_called_once()
        self.assertIn("coingecko", self.http.get.call_args.args[0])
        self.service.get_rate("USD", "ETH")
        self.assertEqual(self.http.get.call_count, 1)
        self.service.get_rate("EUR", "USD")
        self.assertEqual(self.http.get.call_count, 2)
        self.assertIn("exchangerate-api", self.http.get.call_args.args[0])
        result = self.service.get_rate("BTC", "EUR")
        self.assertAlmostEqual(result["rate"], 60000 * 0.927)
        self.assertEqual(self.http.get.call_count, 2)

    def test_cross_refresh_does_not_request_source_for_a_fresh_leg(self):
        self.service.get_rate("BTC", "USD")
        self.http.reset_mock()
        result = self.service.get_rate("BTC", "EUR")
        self.assertAlmostEqual(result["rate"], 60000 * 0.927)
        self.http.get.assert_called_once()
        self.assertIn("exchangerate-api", self.http.get.call_args.args[0])

    def test_expired_cache_refreshes_before_trade_and_failure_preserves_portfolio(self):
        self.service.register("alice", "1234")
        self.service.login("alice", "1234")
        self.service.buy("BTC", 0.05)
        self.service.rates.clock = lambda: NOW + timedelta(minutes=5)
        self.http.get.side_effect = requests.Timeout("must-not-appear-fixture-key")
        original = (self.config.data_dir / "portfolios.json").read_bytes()
        with self.assertRaises(ApiRequestError):
            self.service.sell("BTC", 0.01)
        self.assertEqual(
            (self.config.data_dir / "portfolios.json").read_bytes(), original
        )
        self.assertNotIn(
            "fixture-key", (self.directory / "logs/parser.log").read_text()
        )

    def test_identity_unknown_codes_and_corrupt_cache_never_call_api(self):
        self.assertEqual(self.rates.get_rate("USD", "USD").rate, 1)
        with self.assertRaises(CurrencyNotFoundError):
            self.rates.get_rate("ABC", "USD")
        (self.config.data_dir / "rates.json").write_text("broken")
        with self.assertRaises(StorageError):
            self.rates.get_rate("BTC", "USD")
        self.http.get.assert_not_called()

    def test_no_default_stub_and_production_service_wires_parser(self):
        with self.assertRaises(ApiRequestError):
            RateService(self.storage, clock=lambda: NOW).get_rate("BTC", "USD")
        with patch.dict("os.environ", {"EXCHANGERATE_API_KEY": "test-only"}):
            service = WalletService(self.storage, clock=lambda: NOW)
        with patch("requests.get", side_effect=self.reply) as network:
            self.assertEqual(service.get_rate("BTC", "USD")["rate"], 60000)
            network.assert_called_once()

    def test_missing_fiat_key_allows_crypto_partial_update(self):
        config = ParserConfig(data_dir=self.config.data_dir)
        updater = build_updater(config, session=self.http, clock=lambda: NOW)
        result = updater.run_update()
        self.assertEqual(result.changed_pairs, 3)
        self.assertEqual(set(result.failed_sources), {"exchangerate"})
        self.http.get.assert_called_once()
        self.assertIn("EXCHANGERATE_API_KEY", result.failed_sources["exchangerate"])


class ShowRatesTests(ParserIntegrationFixture):
    def test_empty_cache_missing_currency_and_unknown_base(self):
        self.assertTrue(self.service.show_rates()["empty"])
        self.service.update_rates()
        result = self.service.show_rates(currency="ABC")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["missing"], ["ABC"])
        with self.assertRaises(CurrencyNotFoundError):
            self.service.show_rates(base_currency="ABC")

    def test_filters_top_conversion_and_listing_are_cache_only(self):
        self.service.update_rates()
        self.http.reset_mock()
        result = self.service.show_rates(top=2)
        self.assertEqual(
            [row["pair"] for row in result["rows"]], ["BTC_USD", "ETH_USD"]
        )
        filtered = self.service.show_rates(currency="RUB")
        self.assertEqual([row["pair"] for row in filtered["rows"]], ["RUB_USD"])
        converted = self.service.show_rates(currency="BTC", base_currency="EUR")
        self.assertAlmostEqual(converted["rows"][0]["rate"], 60000 * 0.927)
        self.assertEqual(converted["rows"][0]["source"], "CoinGecko + ExchangeRate-API")
        self.assertEqual(self.service.show_rates(currency="EUR", top=2)["rows"], [])
        self.http.get.assert_not_called()

    def test_stale_quotes_are_marked_and_cross_uses_oldest_timestamp(self):
        self.service.update_rates()
        data = self.storage.load_rates()
        oldest = NOW - timedelta(minutes=6)
        data["pairs"]["EUR_USD"]["updated_at"] = oldest.isoformat()
        self.storage.save_rates(data)
        self.http.reset_mock()
        row = self.service.show_rates(currency="BTC", base_currency="EUR")["rows"][0]
        self.assertTrue(row["stale"])
        self.assertEqual(row["updated_at"], oldest)
        self.http.get.assert_not_called()

    def test_missing_base_leg_is_omitted_and_invalid_top_is_rejected(self):
        self.service.update_rates("coingecko")
        result = self.service.show_rates(base_currency="EUR")
        self.assertEqual(result["rows"], [])
        self.assertIn("BTC", result["missing"])
        for top in (0, -1, True, 1.5):
            with self.subTest(top=top), self.assertRaises(ValueError):
                self.service.show_rates(top=top)

    def test_configured_default_base_and_explicit_identity_filter(self):
        self.service.update_rates()
        self.settings["DEFAULT_BASE_CURRENCY"] = "EUR"
        row = self.service.show_rates(currency="BTC")["rows"][0]
        self.assertEqual(row["pair"], "BTC_EUR")
        row = self.service.show_rates(currency="EUR")["rows"][0]
        self.assertEqual(row["rate"], 1)

    def test_cli_update_show_rates_and_errors_keep_session_running(self):
        commands = iter(
            [
                "show-rates",
                "update-rates",
                "show-rates --top 2",
                "show-rates --currency ABC",
                "show-rates --top 0",
                "exit",
            ]
        )
        output = io.StringIO()
        WalletApplication(
            self.service, input_fn=lambda: next(commands), output=output
        ).run()
        rendered = output.getvalue()
        for expected in (
            "Локальный кеш курсов пуст",
            "Обновлено курсов: 6",
            "BTC_USD",
            "Источник",
            "Курс для 'ABC' не найден в кеше",
            "положительным целым числом",
            "До свидания!",
        ):
            self.assertIn(expected, rendered)
        self.assertEqual(self.http.get.call_count, 2)


class ParserLogTests(ParserIntegrationFixture):
    def test_complete_update_logs_start_source_results_and_persistence(self):
        self.service.update_rates()
        rows = [
            json.loads(line)
            for line in (self.directory / "logs/parser.log").read_text().splitlines()
        ]
        events = [row["event"] for row in rows]
        self.assertEqual(events[0], "update_start")
        self.assertEqual(events.count("fetch_success"), 2)
        self.assertIn("snapshot_saved", events)
        self.assertEqual(events[-1], "update_finished")
        self.assertEqual(rows[-1]["result"], "OK")
        self.assertEqual(rows[-1]["rates_count"], 6)
        self.assertIsInstance(rows[-1]["duration_ms"], int)
        self.assertNotIn(
            "fixture-key", (self.directory / "logs/parser.log").read_text()
        )

    def test_parser_rotation_and_idempotent_setup_do_not_duplicate_records(self):
        settings = {**self.settings, "LOG_MAX_BYTES": 1200, "LOG_BACKUP_COUNT": 2}
        first = setup_parser_logging(settings).handlers[0]
        self.assertIs(setup_parser_logging(settings).handlers[0], first)
        for _ in range(4):
            self.service.update_rates()
        files = list((self.directory / "logs").glob("parser.log*"))
        self.assertEqual(len(files), 3)
        for file in files:
            for line in file.read_text().splitlines():
                self.assertIn("event", json.loads(line))

    def test_bad_source_and_persistence_failures_have_parser_log_records(self):
        self.http.get.side_effect = requests.ConnectionError("fixture-key")
        with self.assertRaises(ApiRequestError):
            self.service.update_rates()
        self.http.get.side_effect = self.reply
        with patch.object(
            self.updater.storage, "commit", side_effect=StorageError("disk full")
        ):
            with self.assertRaises(StorageError):
                self.service.update_rates()
        records = [
            json.loads(line)
            for line in (self.directory / "logs/parser.log").read_text().splitlines()
        ]
        self.assertEqual(
            sum(record["event"] == "fetch_failed" for record in records), 2
        )
        self.assertIn("persistence_failed", {record["event"] for record in records})
        self.assertNotIn(
            "fixture-key", (self.directory / "logs/parser.log").read_text()
        )


class SchedulerTests(unittest.TestCase):
    def test_one_shot_exit_codes(self):
        for failed, expected in (({}, 0), ({"coingecko": "offline"}, 2)):
            updater = Mock()
            updater.run_update.return_value = UpdateResult(
                3, 3, failed, NOW.isoformat()
            )
            with self.subTest(failed=failed):
                self.assertEqual(run_scheduler(updater, report=Mock()), expected)
                updater.run_update.assert_called_once_with(None)
        updater.run_update.side_effect = ApiRequestError("offline")
        self.assertEqual(run_scheduler(updater, report=Mock()), 1)

    def test_watch_waits_after_completion_and_continues_after_api_failures(self):
        events = []
        updater = Mock()
        attempts = iter(
            [ApiRequestError("offline"), UpdateResult(1, 1, {}, NOW.isoformat())]
        )

        def update(source):
            events.append(("update", source))
            outcome = next(attempts)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def wait(interval):
            events.append(("wait", interval))
            return len(events) == 4

        updater.run_update.side_effect = update
        self.assertEqual(
            run_scheduler(
                updater,
                watch=True,
                interval=30,
                source="coingecko",
                wait=wait,
                report=Mock(),
            ),
            0,
        )
        self.assertEqual(
            events,
            [
                ("update", "coingecko"),
                ("wait", 30),
                ("update", "coingecko"),
                ("wait", 30),
            ],
        )

    def test_storage_errors_stop_watch_without_waiting(self):
        updater = Mock()
        updater.run_update.side_effect = StorageError("corrupt file")
        wait = Mock()
        with self.assertRaises(StorageError):
            run_scheduler(updater, watch=True, wait=wait)
        wait.assert_not_called()

    def test_invalid_interval_and_cli_cancellation(self):
        for interval in (0, -1, float("inf"), float("nan"), True, 1e20):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                run_scheduler(Mock(), interval=interval)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            main(["--interval", "10"])
        self.assertEqual(failure.exception.code, 2)
        with patch(
            "valutatrade_hub.parser_service.scheduler.SettingsLoader",
            side_effect=KeyboardInterrupt,
        ):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([]), 0)
            self.assertIn("остановлен", output.getvalue())
