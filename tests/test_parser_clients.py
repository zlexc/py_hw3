"""API-ответы и конфигурация: тесты без HTTP-запросов и реальных ключей."""

import json
import os
import tempfile
import traceback
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from tests.helpers import NOW
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.parser_service.api_clients import (
    BaseApiClient,
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig


def response(payload, *, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result.headers.update(headers or {})
    result.close = Mock()
    return result


def coin_payload():
    return {
        raw_id: {"usd": rate, "last_updated_at": int(NOW.timestamp())}
        for raw_id, rate in (("bitcoin", 60000), ("ethereum", 3000), ("solana", 150))
    }


def fiat_payload(field="conversion_rates"):
    return {
        "result": "success",
        "base_code": "USD",
        "time_last_update_unix": int(NOW.timestamp()),
        field: {"EUR": 0.927, "GBP": 0.809, "RUB": 98.45},
    }


class ParserConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.env = self.directory / ".env"
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_local_keys_and_environment_precedence_are_not_exposed_in_repr(self):
        self.env.write_text(
            '# comment\nEXCHANGERATE_API_KEY="local-secret"\n'
            "COINGECKO_API_KEY='demo-secret'\nUNRELATED=$(not-executed)\n"
        )
        config = ParserConfig.from_settings({}, env_path=self.env)
        self.assertEqual(config.exchangerate_api_key, "local-secret")
        self.assertEqual(config.coingecko_api_key, "demo-secret")
        os.environ["EXCHANGERATE_API_KEY"] = "environment-secret"
        updated = ParserConfig.from_settings({}, env_path=self.env)
        self.assertEqual(updated.exchangerate_api_key, "environment-secret")
        for secret in ("local-secret", "demo-secret", "environment-secret"):
            self.assertNotIn(secret, repr(updated))
        self.assertNotIn("UNRELATED", os.environ)

    def test_missing_optional_file_keys_and_derived_paths(self):
        config = ParserConfig.from_settings(
            {"DATA_DIR": self.directory}, env_path=self.env
        )
        self.assertEqual(config.exchangerate_api_key, "")
        self.assertEqual(config.coingecko_api_key, "")
        self.assertEqual(
            config.rates_file_path, self.directory.resolve() / "rates.json"
        )
        self.assertEqual(config.history_file_path.name, "exchange_rates.json")
        self.assertEqual(config.request_timeout, 10)
        self.assertEqual(config.update_interval_seconds, 3600)
        self.assertFalse(self.env.exists())

    def test_invalid_parser_settings_are_rejected(self):
        cases = (
            {"BASE_CURRENCY": "EUR"},
            {"REQUEST_TIMEOUT": 0},
            {"REQUEST_TIMEOUT": True},
            {"UPDATE_INTERVAL_SECONDS": float("inf")},
            {"UPDATE_INTERVAL_SECONDS": 1e20},
            {"FIAT_CURRENCIES": "EUR"},
            {"FIAT_CURRENCIES": ["EUR", "eur"]},
            {"FIAT_CURRENCIES": ["BTC"]},
            {"CRYPTO_CURRENCIES": ["EUR"]},
            {"CRYPTO_ID_MAP": {}},
            {"COINGECKO_URL": "http://example.com"},
            {"COINGECKO_URL": "https://user:secret@example.com/"},
            {"EXCHANGERATE_API_KEY": "should-not-be-here"},
            {"FIAT_CURRENCIES": [], "CRYPTO_CURRENCIES": []},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                ParserConfig.from_settings({"PARSER": overrides}, env_path=self.env)
        with self.assertRaises(ValueError):
            ParserConfig.from_settings({"PARSER": []}, env_path=self.env)

    def test_valid_overrides_are_normalized(self):
        config = ParserConfig.from_settings(
            {
                "PARSER": {
                    "FIAT_CURRENCIES": ["eur"],
                    "CRYPTO_CURRENCIES": [],
                    "REQUEST_TIMEOUT": 3,
                    "UPDATE_INTERVAL_SECONDS": 120,
                }
            },
            env_path=self.env,
        )
        self.assertEqual(config.fiat_currencies, ("EUR",))
        self.assertEqual(config.crypto_currencies, ())
        self.assertEqual(config.request_timeout, 3)


class ApiClientTests(unittest.TestCase):
    def setUp(self):
        self.config = ParserConfig(
            exchangerate_api_key="private-key", coingecko_api_key="demo-key"
        )
        self.http = Mock()

    def client(self, kind, payload, *, status=200):
        reply = response(payload, status=status, headers={"ETag": 'W/"test"'})
        self.http.get.return_value = reply
        return kind(
            self.config,
            session=self.http,
            clock=lambda: NOW,
            timer=Mock(side_effect=[1.0, 1.125]),
        )

    def test_base_client_is_abstract(self):
        with self.assertRaises(TypeError):
            BaseApiClient(self.config)

    def test_coingecko_batched_ids_headers_timeout_and_metadata(self):
        client = self.client(CoinGeckoClient, coin_payload())
        self.assertEqual(
            client.fetch_rates(), {"BTC_USD": 60000, "ETH_USD": 3000, "SOL_USD": 150}
        )
        self.http.get.assert_called_once()
        request = self.http.get.call_args.kwargs
        self.assertEqual(request["params"]["ids"], "bitcoin,ethereum,solana")
        self.assertEqual(request["params"]["vs_currencies"], "usd")
        self.assertEqual(request["headers"], {"x-cg-demo-api-key": "demo-key"})
        self.assertEqual(request["timeout"], 10)
        self.assertFalse(request["allow_redirects"])
        metadata = client.last_fetch["BTC_USD"]
        self.assertEqual(metadata["raw_id"], "bitcoin")
        self.assertEqual(metadata["request_ms"], 125)
        self.assertEqual(metadata["status_code"], 200)
        self.assertEqual(metadata["etag"], 'W/"test"')
        self.assertTrue(metadata["timestamp"].endswith(".000000Z"))
        self.assertEqual(metadata["source_updated_at"], metadata["timestamp"])
        self.http.get.return_value.close.assert_called_once()

    def test_coingecko_without_key_sends_no_auth_header(self):
        client = self.client(CoinGeckoClient, coin_payload())
        client.config = replace(self.config, coingecko_api_key="")
        client.fetch_rates()
        self.assertEqual(self.http.get.call_args.kwargs["headers"], {})

    def test_exchange_official_and_homework_formats_invert_usd_quotes(self):
        for field in ("conversion_rates", "rates"):
            with self.subTest(field=field):
                self.http.reset_mock()
                client = self.client(ExchangeRateApiClient, fiat_payload(field))
                rates = client.fetch_rates()
                self.assertAlmostEqual(rates["EUR_USD"], 1 / 0.927)
                self.assertAlmostEqual(rates["RUB_USD"], 1 / 98.45)
                self.assertIn(
                    "/private-key/latest/USD", self.http.get.call_args.args[0]
                )
                self.http.get.assert_called_once()
                self.assertIn("source_updated_at", client.last_fetch["EUR_USD"])

    def test_exchange_utc_timestamp_fallback(self):
        payload = fiat_payload()
        payload.pop("time_last_update_unix")
        payload["time_last_update_utc"] = "Sun, 08 Nov 2026 12:00:00 +0000"
        client = self.client(ExchangeRateApiClient, payload)
        client.fetch_rates()
        self.assertIn(
            "2026-11-08T12:00:00", client.last_fetch["EUR_USD"]["source_updated_at"]
        )

    def test_missing_exchange_key_does_not_issue_http_request(self):
        client = ExchangeRateApiClient(
            replace(self.config, exchangerate_api_key=""), session=self.http
        )
        with self.assertRaisesRegex(ApiRequestError, "EXCHANGERATE_API_KEY"):
            client.fetch_rates()
        self.http.get.assert_not_called()

    def test_http_failures_have_sanitized_messages_and_close_responses(self):
        for status in (301, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                client = self.client(
                    ExchangeRateApiClient, {"error": "private-key"}, status=status
                )
                with self.assertRaisesRegex(
                    ApiRequestError, f"HTTP {status}"
                ) as failure:
                    client.fetch_rates()
                self.assertNotIn("private-key", str(failure.exception))
                self.assertEqual(client.last_fetch, {})
                self.http.get.return_value.close.assert_called_once()

    def test_request_exceptions_do_not_reveal_url_keys_even_in_traceback(self):
        for error in (
            requests.Timeout("private-key"),
            requests.ConnectionError("https://private-key"),
        ):
            with self.subTest(error=type(error).__name__):
                self.http.get.side_effect = error
                client = ExchangeRateApiClient(self.config, session=self.http)
                try:
                    client.fetch_rates()
                except ApiRequestError as failure:
                    rendered = "".join(traceback.format_exception(failure))
                    self.assertNotIn("private-key", rendered)
                else:
                    self.fail("Expected ApiRequestError")

    def test_coin_missing_or_invalid_quote_rejects_entire_source(self):
        for invalid in (None, True, 0, -1, "100", float("nan"), float("inf")):
            payload = coin_payload()
            payload["solana"]["usd"] = invalid
            with self.subTest(invalid=invalid):
                client = self.client(CoinGeckoClient, payload)
                with self.assertRaises(ApiRequestError):
                    client.fetch_rates()
                self.assertEqual(client.last_fetch, {})
        payload = coin_payload()
        del payload["ethereum"]
        with self.assertRaises(ApiRequestError):
            self.client(CoinGeckoClient, payload).fetch_rates()

    def test_exchange_failure_flags_wrong_base_and_invalid_quotes(self):
        payloads = [
            {"result": "error", "error-type": "invalid-key"},
            {**fiat_payload(), "base_code": "EUR"},
            {**fiat_payload(), "conversion_rates": {}},
            {**fiat_payload(), "conversion_rates": None},
        ]
        for invalid in (False, 0, -1, "1", float("inf"), None):
            payload = fiat_payload()
            payload["conversion_rates"]["EUR"] = invalid
            payloads.append(payload)
        for payload in payloads:
            with self.subTest(payload=payload):
                client = self.client(ExchangeRateApiClient, payload)
                with self.assertRaises(ApiRequestError):
                    client.fetch_rates()
                self.assertEqual(client.last_fetch, {})

    def test_invalid_json_and_nonobject_payload_are_api_errors(self):
        for payload in ([], "text"):
            with self.assertRaises(ApiRequestError):
                self.client(CoinGeckoClient, payload).fetch_rates()
        self.http.get.return_value = response({})
        self.http.get.return_value._content = b"not JSON private-key"
        with self.assertRaises(ApiRequestError) as failure:
            CoinGeckoClient(self.config, session=self.http).fetch_rates()
        self.assertNotIn("private-key", str(failure.exception))
