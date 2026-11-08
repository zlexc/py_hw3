"""Свежие курсы из кеша; внешнее обновление передаётся как зависимость."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from valutatrade_hub.core import utils
from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    get_currency,
    supported_codes,
)
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.storage import JsonStorage, normalize_rates
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import DEFAULT_SETTINGS, Settings


@dataclass(frozen=True)
class RateQuote:
    rate: float
    updated_at: datetime
    source: str = ""


class RateService:
    """Прямые/обратные/кросс-курсы с TTL и выборочным обновлением источников."""

    def __init__(
        self,
        storage: JsonStorage | DatabaseManager,
        *,
        provider: Callable[[], dict] | None = None,
        refresher: Callable[[set[str]], object] | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(UTC))
        # provider — явно переданные тестовые данные; заглушки по умолчанию нет.
        self.provider = provider
        self.refresher = refresher
        configuration = settings if settings is not None else DEFAULT_SETTINGS
        self.ttl = (
            ttl
            if ttl is not None
            else timedelta(seconds=configuration.get("RATES_TTL_SECONDS", 300))
        )
        if self.ttl <= timedelta(0):
            raise ValueError("TTL курсов должен быть положительным")

    @property
    def supported_currencies(self) -> set[str]:
        return set(supported_codes())

    def is_fresh(self, timestamp: datetime) -> bool:
        age = utils.utc_time(self.clock()) - timestamp
        return timedelta(0) <= age < self.ttl

    def _quote(self, pairs, pair, *, allow_stale=False):
        entry = pairs.get(pair)
        if entry is None:
            return None
        timestamp = utils.parse_time(entry["updated_at"])
        if not allow_stale and not self.is_fresh(timestamp):
            return None
        return RateQuote(entry["rate"], timestamp, entry["source"])

    def _direct(self, pairs, source, target, *, allow_stale=False):
        direct = self._quote(pairs, f"{source}_{target}", allow_stale=allow_stale)
        if direct is not None:
            return direct
        inverse = self._quote(pairs, f"{target}_{source}", allow_stale=allow_stale)
        if inverse is not None:
            try:
                rate = utils.number(1 / inverse.rate, positive=True)
            except ValueError:
                return None
            return RateQuote(rate, inverse.updated_at, inverse.source)
        return None

    def cached_rate(
        self,
        source: str,
        target: str,
        *,
        snapshot: dict | None = None,
        allow_stale: bool = False,
    ) -> RateQuote | None:
        source, target = get_currency(source).code, get_currency(target).code
        data = snapshot if snapshot is not None else self.storage.load_rates()
        pairs = data["pairs"]
        if source == target:
            return RateQuote(1.0, utils.utc_time(self.clock()), "Identity")
        direct = self._direct(pairs, source, target, allow_stale=allow_stale)
        if direct is not None:
            return direct
        if "USD" in (source, target):
            return None
        first = self._direct(pairs, source, "USD", allow_stale=allow_stale)
        second = self._direct(pairs, "USD", target, allow_stale=allow_stale)
        if first is None or second is None:
            return None
        try:
            rate = utils.number(first.rate * second.rate, positive=True)
        except ValueError:
            return None
        origin = " + ".join(sorted({first.source, second.source}))
        return RateQuote(rate, min(first.updated_at, second.updated_at), origin)

    def _needed_sources(self, source, target, snapshot):
        result = set()
        for code in (source, target):
            if code == "USD" or self.cached_rate(code, "USD", snapshot=snapshot):
                continue
            kind = (
                "coingecko"
                if isinstance(get_currency(code), CryptoCurrency)
                else "exchangerate"
            )
            result.add(kind)
        # Например, в кеше есть только устаревшая прямая кросс-пара.
        if not result:
            result = {
                "coingecko"
                if isinstance(get_currency(code), CryptoCurrency)
                else "exchangerate"
                for code in (source, target)
                if code != "USD"
            }
        return result

    def get_rate(self, from_currency: str, to_currency: str) -> RateQuote:
        with self.storage.operation():
            source, target = (
                get_currency(from_currency).code,
                get_currency(to_currency).code,
            )
            cached = self.storage.load_rates()
            quote = self.cached_rate(source, target, snapshot=cached)
            if quote is not None:
                return quote
            message = f"Курс {source}→{target} недоступен"
            if self.refresher is not None:
                self.refresher(self._needed_sources(source, target, cached))
            elif self.provider is not None:
                try:
                    refreshed = normalize_rates(self.provider())
                except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                    raise ApiRequestError(message) from None
                quote = self.cached_rate(source, target, snapshot=refreshed)
                if quote is None:
                    raise ApiRequestError(message)
                self.storage.save_rates(refreshed)
            else:
                raise ApiRequestError(message + ". Выполните update-rates")
            quote = self.cached_rate(source, target)
            if quote is None:
                raise ApiRequestError(message + ". Выполните update-rates")
            return quote
