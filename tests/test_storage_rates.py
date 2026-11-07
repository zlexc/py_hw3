"""JSON-сохранение и получение свежих курсов без сети."""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import NOW, make_user
from valutatrade_hub.core.models import Portfolio, Wallet
from valutatrade_hub.core.rates import RateService, RateUnavailableError
from valutatrade_hub.core.storage import JsonStorage, StorageError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.storage = JsonStorage(self.directory)

    def test_round_trip_users_and_portfolios(self):
        user = make_user(username="Алиса")
        self.storage.save_users([user])
        self.storage.save_portfolios(
            {1: Portfolio(1, user, {"BTC": Wallet("BTC", 0.05)})}
        )
        restored = JsonStorage(self.directory)
        users = restored.load_users()
        self.assertTrue(users[0].verify_password("1234"))
        self.assertEqual(users[0].username, "Алиса")
        self.assertEqual(users[0].registration_date, NOW)
        portfolio = restored.load_portfolios(users)[1]
        self.assertEqual(portfolio.get_wallet("BTC").balance, 0.05)

    def test_corrupt_or_invalid_files_are_preserved(self):
        cases = [
            ("users.json", "not JSON"),
            ("users.json", "{}"),
            ("users.json", '[{"user_id": 1}]'),
            ("portfolios.json", "{}"),
            ("portfolios.json", '[{"user_id": 8, "wallets": {}}]'),
            ("rates.json", "[]"),
            ("rates.json", '{"BTC_USD": {"rate": -1, "updated_at": "bad"}}'),
        ]
        for filename, text in cases:
            with self.subTest(filename=filename, text=text):
                target = self.directory / filename
                original = target.read_bytes()
                target.write_text(text, encoding="utf-8")
                with self.assertRaises(StorageError):
                    JsonStorage(self.directory)
                self.assertEqual(target.read_text(encoding="utf-8"), text)
                target.write_bytes(original)

    def test_duplicate_users_and_negative_balances_are_rejected(self):
        user = make_user()
        self.storage.save_users([user, user])
        with self.assertRaisesRegex(StorageError, "повторяющийся"):
            self.storage.load_users()
        self.storage.save_users([user])
        payload = [{"user_id": 1, "wallets": {"USD": {"balance": -1}}}]
        (self.directory / "portfolios.json").write_text(json.dumps(payload))
        with self.assertRaises(StorageError):
            self.storage.load_portfolios([user])

    def test_failed_atomic_replacement_preserves_existing_file(self):
        original = (self.directory / "users.json").read_bytes()
        with patch("valutatrade_hub.core.storage.os.replace", side_effect=OSError):
            with self.assertRaises(StorageError):
                self.storage.save_users([make_user()])
        self.assertEqual((self.directory / "users.json").read_bytes(), original)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])


class RateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = JsonStorage(self.temporary.name)
        self.provider = Mock(side_effect=OSError("offline"))
        self.service = RateService(
            self.storage, provider=self.provider, clock=lambda: NOW
        )

    def cache(self, **pairs):
        self.storage.save_rates(
            {
                pair: {"rate": rate, "updated_at": timestamp.isoformat()}
                for pair, (rate, timestamp) in pairs.items()
            }
        )

    def test_fresh_direct_and_inverse_without_provider(self):
        self.cache(EUR_USD=(1.2, NOW - timedelta(seconds=30)))
        self.assertEqual(self.service.get_rate("eur", "usd").rate, 1.2)
        self.assertAlmostEqual(self.service.get_rate("USD", "EUR").rate, 1 / 1.2)
        self.provider.assert_not_called()

    def test_cross_rate_uses_oldest_timestamp(self):
        older = NOW - timedelta(minutes=2)
        self.cache(BTC_USD=(60000, older), EUR_USD=(1.2, NOW))
        quote = self.service.get_rate("BTC", "EUR")
        self.assertEqual(quote.rate, 50000)
        self.assertEqual(quote.updated_at, older)

    def test_refresh_missing_and_expired_rates_with_stub(self):
        self.cache(BTC_USD=(1, NOW - timedelta(minutes=5)))
        service = RateService(self.storage, clock=lambda: NOW)
        quote = service.get_rate("USD", "BTC")
        self.assertAlmostEqual(quote.rate, 1 / 59337.21)
        self.assertEqual(quote.updated_at, NOW)
        self.assertEqual(self.storage.load_rates()["source"], "Stub")
        self.assertEqual(self.storage.load_rates()["last_refresh"], NOW.isoformat())

    def test_refresh_timestamp_can_follow_start_of_request(self):
        clock = Mock(
            side_effect=[NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)]
        )
        service = RateService(self.storage, clock=clock)
        quote = service.get_rate("BTC", "USD")
        self.assertEqual(quote.rate, 59337.21)
        self.assertEqual(quote.updated_at, NOW + timedelta(seconds=1))

    def test_failed_refresh_never_returns_stale_quote_or_changes_cache(self):
        for timestamp in (NOW - timedelta(minutes=5), NOW + timedelta(seconds=1)):
            with self.subTest(timestamp=timestamp):
                self.cache(BTC_USD=(60000, timestamp))
                original = self.storage.load_rates()
                with self.assertRaisesRegex(RateUnavailableError, "Повторите попытку"):
                    self.service.get_rate("BTC", "USD")
                self.assertEqual(self.storage.load_rates(), original)

    def test_unknown_and_identical_currencies(self):
        self.assertEqual(self.service.get_rate("BTC", "BTC").rate, 1)
        self.provider.assert_not_called()
        for source, target in (("ABC", "USD"), ("ABC", "ABC")):
            with self.subTest(source=source, target=target):
                with self.assertRaises(RateUnavailableError):
                    self.service.get_rate(source, target)

    def test_malformed_provider_response_does_not_overwrite_cache(self):
        self.cache(BTC_USD=(60000, NOW - timedelta(minutes=10)))
        original = self.storage.load_rates()
        self.provider.side_effect = None
        self.provider.return_value = {"BTC_USD": {"rate": True, "updated_at": "bad"}}
        with self.assertRaises(RateUnavailableError):
            self.service.get_rate("BTC", "USD")
        self.assertEqual(self.storage.load_rates(), original)
