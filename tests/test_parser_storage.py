"""История, атомарная публикация, миграция и независимые ошибки источников."""

import json
import logging
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import NOW
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.storage import JsonStorage, StorageError, write_json
from valutatrade_hub.logging_config import PARSER_LOGGER_NAME
from valutatrade_hub.parser_service.api_clients import iso_utc
from valutatrade_hub.parser_service.storage import ParserStorage
from valutatrade_hub.parser_service.updater import RatesUpdater


def measurement(code="BTC", rate=60000, timestamp=NOW, source="CoinGecko"):
    timestamp = iso_utc(timestamp)
    return {
        "id": f"{code}_USD_{timestamp}",
        "from_currency": code,
        "to_currency": "USD",
        "rate": rate,
        "timestamp": timestamp,
        "source": source,
        "meta": {"request_ms": 10, "status_code": 200},
    }


def fake_client(key="coingecko", quotes=None, timestamp=NOW):
    client = Mock()
    client.source_key = key
    client.source = "CoinGecko" if key == "coingecko" else "ExchangeRate-API"
    quotes = quotes if quotes is not None else {"BTC_USD": 60000}
    client.fetch_rates.return_value = quotes
    client.last_fetch = {
        pair: {"timestamp": iso_utc(timestamp), "request_ms": 10, "status_code": 200}
        for pair in quotes
    }
    return client


class ParserStorageFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.storage = ParserStorage(self.directory)
        self.logger = logging.getLogger(PARSER_LOGGER_NAME)
        handler = logging.NullHandler()
        self.logger.addHandler(handler)
        self.addCleanup(self.logger.removeHandler, handler)


class ParserStorageTests(ParserStorageFixture):
    def test_history_deduplication_and_newer_timestamp_wins(self):
        initial = measurement()
        changed, added, _ = self.storage.commit([initial], NOW)
        self.assertEqual((changed, added), (1, 1))
        self.assertEqual(self.storage.commit([initial], NOW)[:2], (0, 0))
        older = measurement(rate=1, timestamp=NOW - timedelta(minutes=1))
        self.assertEqual(self.storage.commit([older], NOW)[:2], (0, 1))
        cached = self.storage.load_cache()
        self.assertEqual(cached["pairs"]["BTC_USD"]["rate"], 60000)
        self.assertEqual(len(self.storage.load_history()), 2)
        newer = measurement(rate=61000, timestamp=NOW + timedelta(seconds=1))
        self.assertEqual(self.storage.commit([newer], NOW)[:2], (1, 1))
        self.assertEqual(self.storage.load_cache()["pairs"]["BTC_USD"]["rate"], 61000)

    def test_merge_preserves_other_source_and_last_refresh_never_moves_backwards(self):
        self.storage.commit([measurement()], NOW + timedelta(hours=1))
        self.storage.commit([measurement("EUR", 1.2, source="ExchangeRate-API")], NOW)
        cached = self.storage.load_cache()
        self.assertEqual(set(cached["pairs"]), {"BTC_USD", "EUR_USD"})
        self.assertEqual(
            cached["last_refresh"],
            iso_utc(NOW + timedelta(hours=1)).replace(".000000Z", "+00:00"),
        )

    def test_legacy_cache_is_read_without_rewriting_and_migrates_after_success(self):
        legacy = {
            "EUR_USD": {"rate": 1.2, "updated_at": NOW.isoformat()},
            "source": "LegacySource",
            "last_refresh": NOW.isoformat(),
        }
        target = self.directory / "rates.json"
        target.write_text(json.dumps(legacy))
        original = target.read_bytes()
        cached = self.storage.load_cache()
        self.assertEqual(cached["pairs"]["EUR_USD"]["source"], "LegacySource")
        self.assertEqual(target.read_bytes(), original)
        self.storage.commit([measurement()], NOW)
        stored = json.loads(target.read_text())
        self.assertEqual(set(stored), {"pairs", "last_refresh"})
        self.assertEqual(set(stored["pairs"]), {"BTC_USD", "EUR_USD"})

    def test_corrupt_cache_or_history_is_not_overwritten(self):
        self.storage.commit([measurement()], NOW)
        for filename, text in (
            ("rates.json", "not JSON"),
            ("rates.json", '{"pairs": []}'),
            ("exchange_rates.json", "{}"),
            ("exchange_rates.json", '[{"id": "bad"}]'),
        ):
            target = self.directory / filename
            original = target.read_bytes()
            target.write_text(text)
            before = {
                path.name: path.read_bytes() for path in self.directory.glob("*.json")
            }
            with (
                self.subTest(filename=filename, text=text),
                self.assertRaises(StorageError),
            ):
                self.storage.commit([measurement("ETH", 3000)], NOW)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in self.directory.glob("*.json")
                },
                before,
            )
            target.write_bytes(original)

    def test_failed_history_replace_preserves_both_files(self):
        self.storage.commit([measurement()], NOW)
        before = {
            path.name: path.read_bytes() for path in self.directory.glob("*.json")
        }
        with patch(
            "valutatrade_hub.core.storage.os.replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(StorageError):
                self.storage.commit([measurement("ETH", 3000)], NOW)
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.directory.glob("*.json")},
            before,
        )
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_failed_cache_publication_can_leave_history_ahead_and_retry_recovers(self):
        self.storage.commit([measurement()], NOW)
        original = (self.directory / "rates.json").read_bytes()

        def fail_cache(path, data):
            if path.name == "rates.json":
                raise StorageError("cannot publish")
            write_json(path, data)

        entry = measurement("ETH", 3000)
        with patch(
            "valutatrade_hub.parser_service.storage.write_json", side_effect=fail_cache
        ):
            with self.assertRaises(StorageError):
                self.storage.commit([entry], NOW)
        self.assertEqual((self.directory / "rates.json").read_bytes(), original)
        self.assertEqual(len(self.storage.load_history()), 2)
        self.assertEqual(self.storage.commit([entry], NOW)[:2], (1, 0))
        self.assertIn("ETH_USD", self.storage.load_cache()["pairs"])

    def test_separate_processes_and_core_initialization_do_not_lose_updates(self):
        script = """
import sys
from datetime import UTC, datetime
from pathlib import Path
from valutatrade_hub.core.storage import JsonStorage
from valutatrade_hub.parser_service.storage import ParserStorage
path = Path(sys.argv[1])
code = sys.argv[2]
if code == 'init':
    JsonStorage(path)
else:
    timestamp = '2026-11-08T12:00:00.000000Z'
    entry = {'id': f'{code}_USD_{timestamp}', 'from_currency': code,
             'to_currency': 'USD', 'rate': 1, 'timestamp': timestamp,
             'source': 'Test', 'meta': {}}
    ParserStorage(path).commit([entry], datetime.now(UTC))
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.directory), code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for code in ("BTC", "EUR", "ETH", "SOL", "RUB", "GBP", "init")
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(len(self.storage.load_cache()["pairs"]), 6)
        self.assertEqual(len(self.storage.load_history()), 6)
        self.assertEqual(JsonStorage(self.directory).load_users(), [])

    def test_parser_does_not_need_or_modify_user_files(self):
        target = self.directory / "users.json"
        target.write_text("unrelated corrupt file")
        self.storage.commit([measurement()], NOW)
        self.assertEqual(target.read_text(), "unrelated corrupt file")
        self.assertFalse((self.directory / "portfolios.json").exists())


class UpdaterTests(ParserStorageFixture):
    def test_both_sources_success_metadata_counts_and_source_selection(self):
        coin = fake_client()
        fiat = fake_client("exchangerate", {"EUR_USD": 1.2})
        updater = RatesUpdater([coin, fiat], self.storage, clock=lambda: NOW)
        result = updater.run_update("coingecko")
        self.assertEqual((result.changed_pairs, result.history_added), (1, 1))
        fiat.fetch_rates.assert_not_called()
        result = updater.run_update()
        self.assertEqual((result.changed_pairs, result.history_added), (1, 1))
        self.assertEqual(result.failed_sources, {})
        self.assertEqual(self.storage.load_history()[0]["meta"]["status_code"], 200)

    def test_one_failed_source_keeps_its_previous_pairs_and_updates_other_source(self):
        self.storage.commit([measurement("EUR", 1.1, source="ExchangeRate-API")], NOW)
        coin = fake_client()
        fiat = fake_client("exchangerate", {"EUR_USD": 1.2})
        fiat.fetch_rates.side_effect = ApiRequestError("ExchangeRate-API: HTTP 429")
        result = RatesUpdater(
            [fiat, coin], self.storage, clock=lambda: NOW
        ).run_update()
        self.assertEqual(set(result.failed_sources), {"exchangerate"})
        self.assertEqual(result.changed_pairs, 1)
        self.assertEqual(self.storage.load_cache()["pairs"]["EUR_USD"]["rate"], 1.1)
        self.assertEqual(self.storage.load_cache()["pairs"]["BTC_USD"]["rate"], 60000)

    def test_all_failed_sources_do_not_write_files(self):
        client = fake_client()
        client.fetch_rates.side_effect = ApiRequestError("offline")
        with self.assertRaises(ApiRequestError):
            RatesUpdater([client], self.storage).run_update()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_corrupt_history_stops_before_any_network_calls(self):
        (self.directory / "exchange_rates.json").write_text("broken")
        client = fake_client()
        with self.assertRaises(StorageError):
            RatesUpdater([client], self.storage).run_update()
        client.fetch_rates.assert_not_called()

    def test_disabled_source_or_missing_metadata_does_not_publish(self):
        client = fake_client()
        updater = RatesUpdater([client], self.storage)
        with self.assertRaises(ApiRequestError):
            updater.run_update("exchangerate")
        client.last_fetch = {}
        with self.assertRaises(ApiRequestError):
            updater.run_update()
        self.assertEqual(list(self.directory.iterdir()), [])
