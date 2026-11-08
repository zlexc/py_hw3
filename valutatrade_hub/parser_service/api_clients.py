"""HTTP-клиенты: единый формат курсов, валидированные данные и безопасные ошибки."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import perf_counter
from urllib.parse import quote

import requests

from valutatrade_hub.core import utils
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.parser_service.config import ParserConfig


def iso_utc(value: datetime) -> str:
    """Сформировать UTC ISO-дату с микросекундами и суффиксом Z."""
    return (
        utils.utc_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def provider_time(value) -> str:
    """Преобразовать Unix-время или HTTP-дату провайдера в UTC ISO-строку."""
    if isinstance(value, str):
        return iso_utc(parsedate_to_datetime(value))
    return iso_utc(datetime.fromtimestamp(utils.number(value, positive=True), UTC))


class BaseApiClient(ABC):
    """Контракт клиента: все настроенные курсы или безопасный ApiRequestError."""

    source: str
    source_key: str

    def __init__(
        self,
        config: ParserConfig,
        *,
        session=None,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] = perf_counter,
    ) -> None:
        self.config = config
        self.session = session if session is not None else requests
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timer = timer
        self.last_fetch: dict[str, dict] = {}

    def _request(self, url: str, *, params=None, headers=None) -> tuple[dict, dict]:
        started = self.timer()
        try:
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.config.request_timeout,
                allow_redirects=False,
            )
        except requests.exceptions.Timeout:
            raise ApiRequestError(f"{self.source}: превышено время ожидания") from None
        except requests.exceptions.RequestException:
            raise ApiRequestError(f"{self.source}: ошибка сети") from None
        try:
            status = response.status_code
            if status != 200:
                reasons = {
                    401: "неверный или отсутствующий ключ API",
                    403: "доступ запрещён",
                    429: "превышен лимит запросов; повторите позже",
                }
                reason = reasons.get(status, "сервис вернул ошибку")
                raise ApiRequestError(f"{self.source}: HTTP {status}, {reason}")
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                raise ApiRequestError(f"{self.source}: некорректный JSON") from None
            metadata = {
                "timestamp": iso_utc(self.clock()),
                "request_ms": max(0, round((self.timer() - started) * 1000)),
                "status_code": status,
            }
            etag = response.headers.get("ETag")
            if isinstance(etag, str):
                metadata["etag"] = etag
            return payload, metadata
        finally:
            response.close()

    @abstractmethod
    def fetch_rates(self) -> dict[str, float]:
        """Вернуть все настроенные пары либо ApiRequestError без частичного ответа."""


class CoinGeckoClient(BaseApiClient):
    """Пакетное получение цен криптовалют в USD по CoinGecko ID."""

    source = "CoinGecko"
    source_key = "coingecko"

    def fetch_rates(self) -> dict[str, float]:
        """Получить все курсы; неполный ответ — ApiRequestError."""
        self.last_fetch = {}
        headers = {}
        if self.config.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self.config.coingecko_api_key
        payload, metadata = self._request(
            self.config.coingecko_url,
            params={
                "ids": ",".join(
                    self.config.crypto_id_map[code]
                    for code in self.config.crypto_currencies
                ),
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
            headers=headers,
        )
        rates, details = {}, {}
        try:
            for code in self.config.crypto_currencies:
                raw_id = self.config.crypto_id_map[code]
                entry = payload[raw_id]
                rate = utils.number(entry["usd"], positive=True)
                pair = f"{code}_USD"
                rates[pair] = rate
                details[pair] = {**metadata, "raw_id": raw_id}
                if "last_updated_at" in entry:
                    details[pair]["source_updated_at"] = provider_time(
                        entry["last_updated_at"]
                    )
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            raise ApiRequestError(
                "CoinGecko: некорректные или неполные курсы"
            ) from None
        self.last_fetch = details
        return rates


class ExchangeRateApiClient(BaseApiClient):
    """Получение фиатных котировок с инверсией USD-курсов в валюту→USD."""

    source = "ExchangeRate-API"
    source_key = "exchangerate"

    def fetch_rates(self) -> dict[str, float]:
        """Получить все курсы; неполный ответ — ApiRequestError."""
        self.last_fetch = {}
        if not self.config.exchangerate_api_key:
            raise ApiRequestError(
                "ExchangeRate-API: настройте EXCHANGERATE_API_KEY в .env"
            )
        key = quote(self.config.exchangerate_api_key, safe="")
        url = f"{self.config.exchangerate_api_url.rstrip('/')}/{key}/latest/USD"
        payload, metadata = self._request(url)
        if payload.get("result") != "success":
            raise ApiRequestError(
                "ExchangeRate-API: API отклонил запрос; проверьте ключ и лимиты"
            )
        try:
            if payload["base_code"] != "USD":
                raise ValueError
            quotes = payload.get("conversion_rates", payload.get("rates"))
            if not isinstance(quotes, dict):
                raise ValueError
            if "time_last_update_unix" in payload:
                metadata["source_updated_at"] = provider_time(
                    payload["time_last_update_unix"]
                )
            elif "time_last_update_utc" in payload:
                metadata["source_updated_at"] = provider_time(
                    payload["time_last_update_utc"]
                )
            rates = {
                f"{code}_USD": utils.number(
                    1 / utils.number(quotes[code], positive=True), positive=True
                )
                for code in self.config.fiat_currencies
            }
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            raise ApiRequestError(
                "ExchangeRate-API: некорректные или неполные курсы"
            ) from None
        self.last_fetch = {pair: metadata.copy() for pair in rates}
        return rates
