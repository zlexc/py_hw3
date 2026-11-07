"""Настройки, Singleton и согласованные операции хранилища."""

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import NOW
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.storage import JsonStorage, StorageError
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader


class SingletonFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "config.json"
        for singleton in (SettingsLoader, DatabaseManager):
            reset = patch.object(singleton, "_instance", None)
            reset.start()
            self.addCleanup(reset.stop)

    def configure(self, **values):
        self.config.write_text(json.dumps(values), encoding="utf-8")
        return SettingsLoader(self.config)


class SettingsTests(SingletonFixture):
    def test_defaults_and_missing_key(self):
        settings = SettingsLoader(self.config)
        self.assertEqual(settings.get("RATES_TTL_SECONDS"), 300)
        self.assertEqual(settings.get("DEFAULT_BASE_CURRENCY"), "USD")
        self.assertEqual(settings.get("LOG_FORMAT"), "json")
        self.assertEqual(settings.get("DATA_DIR"), Path("data").resolve())
        marker = object()
        self.assertIs(settings.get("missing", marker), marker)
        self.assertFalse(self.config.exists())

    def test_identity_cache_and_nested_values_cannot_be_mutated(self):
        first = self.configure(RATES_TTL_SECONDS=60, extra={"value": 1})
        self.config.write_text('{"RATES_TTL_SECONDS": 90}')
        self.assertIs(SettingsLoader(), first)
        self.assertIs(SettingsLoader(self.config), first)
        self.assertEqual(first.get("RATES_TTL_SECONDS"), 60)
        copy = first.get("extra")
        copy["value"] = 2
        self.assertEqual(first.get("extra"), {"value": 1})
        with self.assertRaisesRegex(ValueError, "другого файла"):
            SettingsLoader(self.directory / "other.json")

    def test_invalid_configuration_can_be_corrected_after_failed_initialization(self):
        cases = [
            "not JSON",
            "[]",
            '{"RATES_TTL_SECONDS": 0}',
            '{"RATES_TTL_SECONDS": true}',
            '{"RATES_TTL_SECONDS": "30"}',
            '{"RATES_TTL_SECONDS": NaN}',
            '{"RATES_TTL_SECONDS": 1e308}',
            '{"DEFAULT_BASE_CURRENCY": "ABC"}',
            '{"LOG_LEVEL": "TRACE"}',
            '{"LOG_FORMAT": "text"}',
            '{"LOG_MAX_BYTES": 0}',
            '{"LOG_MAX_BYTES": true}',
            '{"LOG_BACKUP_COUNT": -1}',
            '{"LOG_BACKUP_COUNT": 1.5}',
            '{"LOG_FILE": "../other.log"}',
            '{"DATA_DIR": ""}',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(ValueError):
                    SettingsLoader(self.config)
                self.assertIsNone(SettingsLoader._instance)
                self.assertEqual(self.config.read_text(), text)
        settings = self.configure(LOG_LEVEL="DEBUG", LOG_BACKUP_COUNT=0)
        self.assertEqual(settings.get("LOG_LEVEL"), "DEBUG")

    def test_relative_paths_use_launch_directory_not_config_directory(self):
        settings = self.configure(DATA_DIR="custom-data", LOG_DIR="custom-logs")
        self.assertEqual(settings.get("DATA_DIR"), Path("custom-data").resolve())
        self.assertEqual(settings.get("LOG_DIR"), Path("custom-logs").resolve())

    def test_concurrent_creation_loads_only_once(self):
        self.config.write_text("{}")
        original_load = SettingsLoader._load
        with patch.object(
            SettingsLoader, "_load", autospec=True, side_effect=original_load
        ) as load:
            with ThreadPoolExecutor(max_workers=8) as executor:
                instances = list(
                    executor.map(lambda _: SettingsLoader(self.config), range(20))
                )
        self.assertEqual(len({id(instance) for instance in instances}), 1)
        load.assert_called_once()

    def test_imports_do_not_create_instances_files_or_handlers(self):
        script = """
import importlib
import logging
from pathlib import Path
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.logging_config import ACTION_LOGGER_NAME
for name in ('valutatrade_hub.core.usecases', 'valutatrade_hub.cli.interface',
             'valutatrade_hub.decorators'):
    importlib.import_module(name)
assert SettingsLoader._instance is None
assert DatabaseManager._instance is None
assert not logging.getLogger(ACTION_LOGGER_NAME).handlers
assert list(Path.cwd().iterdir()) == []
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.directory,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class DatabaseTests(SingletonFixture):
    def test_manager_is_singleton_and_preserves_data_on_repeated_initialization(self):
        settings = self.configure(DATA_DIR=str(self.directory / "accounts"))
        manager = DatabaseManager(settings)
        service = WalletService(settings=settings, clock=lambda: NOW)
        service.register("alice", "1234")
        self.assertIs(service.storage, manager)
        self.assertIs(DatabaseManager(), manager)
        self.assertIs(DatabaseManager(settings), manager)
        self.assertEqual(manager.load_users()[0].username, "alice")
        self.assertEqual(manager.data_dir, (self.directory / "accounts").resolve())
        with self.assertRaisesRegex(ValueError, "другой каталог"):
            DatabaseManager({"DATA_DIR": self.directory / "other"})

    def test_operation_context_is_reentrant_and_serializes_registrations(self):
        settings = self.configure(DATA_DIR=str(self.directory / "accounts"))
        manager = DatabaseManager(settings)
        with manager.operation():
            with manager.operation():
                self.assertEqual(manager.load_users(), [])

        def register(index):
            service = WalletService(settings=settings, clock=lambda: NOW)
            return service.register(f"user-{index}", "1234")

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(register, range(12)))
        users = manager.load_users()
        self.assertEqual({user.user_id for user in users}, set(range(1, 13)))
        self.assertEqual(len(manager.load_portfolios(users)), 12)

    def test_corrupt_database_is_preserved_and_failed_instance_is_not_cached(self):
        data_dir = self.directory / "accounts"
        data_dir.mkdir()
        target = data_dir / "users.json"
        target.write_text("broken")
        settings = self.configure(DATA_DIR=str(data_dir))
        with self.assertRaises(StorageError):
            DatabaseManager(settings)
        self.assertIsNone(DatabaseManager._instance)
        self.assertEqual(target.read_text(), "broken")
        target.write_text("[]")
        self.assertEqual(DatabaseManager(settings).load_users(), [])

    def test_configured_base_ttl_and_explicit_injection(self):
        settings = self.configure(
            DATA_DIR=str(self.directory / "accounts"),
            DEFAULT_BASE_CURRENCY="eur",
            RATES_TTL_SECONDS=60,
        )
        service = WalletService(settings=settings, clock=lambda: NOW)
        service.register("alice", "1234")
        service.login("alice", "1234")
        service.buy("USD", 120)
        self.assertEqual(service.rates.ttl, timedelta(seconds=60))
        result = service.show_portfolio()
        self.assertEqual(result["base_currency"], "EUR")
        self.assertAlmostEqual(result["total"], 120 / 1.0786)
        isolated = JsonStorage(self.directory / "isolated")
        injected = WalletService(isolated, clock=lambda: NOW)
        self.assertEqual(isolated.load_users(), [])
        self.assertEqual(injected.rates.ttl, timedelta(minutes=5))
        self.assertIs(injected.storage, isolated)

    def test_configured_ttl_and_explicit_ttl_override(self):
        storage = JsonStorage(self.directory / "cache")
        timestamp = NOW - timedelta(seconds=60)
        storage.save_rates(
            {"EUR_USD": {"rate": 1.2, "updated_at": timestamp.isoformat()}}
        )
        provider = Mock(side_effect=OSError("offline"))
        service = RateService(
            storage,
            provider=provider,
            clock=lambda: NOW,
            settings={"RATES_TTL_SECONDS": 60},
        )
        with self.assertRaises(ApiRequestError):
            service.get_rate("EUR", "USD")
        provider.assert_called_once()
        explicit = RateService(
            storage,
            provider=provider,
            clock=lambda: NOW,
            settings={"RATES_TTL_SECONDS": 60},
            ttl=timedelta(seconds=120),
        )
        self.assertEqual(explicit.get_rate("EUR", "USD").rate, 1.2)
        self.assertEqual(provider.call_count, 1)
