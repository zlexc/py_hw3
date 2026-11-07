"""Контракт курсов и обновляемый кеш с локальным провайдером-заглушкой."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from valutatrade_hub.core import utils
from valutatrade_hub.core.currencies import get_currency, supported_codes
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.storage import JsonStorage
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import DEFAULT_SETTINGS, Settings


@dataclass(frozen=True)
class RateQuote:
    rate: float
    updated_at: datetime


class RateService:
    """Свежие прямые, обратные и кросс-курсы через USD."""

    def __init__(
        self,
        storage: JsonStorage | DatabaseManager,
        *,
        provider: Callable[[], dict] | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(UTC))
        self.provider = provider or self._stub_rates
        configuration = settings if settings is not None else DEFAULT_SETTINGS
        self.ttl = (
            ttl
            if ttl is not None
            else timedelta(seconds=configuration.get("RATES_TTL_SECONDS", 300))
        )
        if self.ttl <= timedelta(0):
            raise ValueError("TTL курсов должен быть положительным")

    def _stub_rates(self) -> dict:
        timestamp = utils.utc_time(self.clock()).isoformat()
        return {
            **{
                f"{code}_USD": {"rate": rate, "updated_at": timestamp}
                for code, rate in utils.DEFAULT_USD_RATES.items()
                if code != "USD"
            },
            "source": "Stub",
            "last_refresh": timestamp,
        }

    @property
    def supported_currencies(self) -> set[str]:
        return set(supported_codes())

    def _fresh(self, data: dict, pair: str, now: datetime) -> RateQuote | None:
        entry = data.get(pair)
        if entry is None:
            return None
        timestamp = utils.parse_time(entry["updated_at"])
        if timedelta(0) <= now - timestamp < self.ttl:
            return RateQuote(float(entry["rate"]), timestamp)
        return None

    def _direct(
        self, data: dict, source: str, target: str, now: datetime
    ) -> RateQuote | None:
        direct = self._fresh(data, f"{source}_{target}", now)
        if direct is not None:
            return direct
        inverse = self._fresh(data, f"{target}_{source}", now)
        if inverse is not None:
            try:
                rate = utils.number(1 / inverse.rate, positive=True)
            except (ValueError, OverflowError):
                return None
            return RateQuote(rate, inverse.updated_at)
        return None

    def _find(
        self, data: dict, source: str, target: str, now: datetime
    ) -> RateQuote | None:
        direct = self._direct(data, source, target, now)
        if direct is not None:
            return direct
        if "USD" in (source, target):
            return None
        source_usd = self._direct(data, source, "USD", now)
        usd_target = self._direct(data, "USD", target, now)
        if source_usd is None or usd_target is None:
            return None
        try:
            rate = utils.number(source_usd.rate * usd_target.rate, positive=True)
        except ValueError:
            return None
        return RateQuote(rate, min(source_usd.updated_at, usd_target.updated_at))

    def get_rate(self, from_currency: str, to_currency: str) -> RateQuote:
        with self.storage.operation():
            return self._get_rate(from_currency, to_currency)

    def _get_rate(self, from_currency: str, to_currency: str) -> RateQuote:
        source = get_currency(from_currency).code
        target = get_currency(to_currency).code
        now = utils.utc_time(self.clock())
        if source == target:
            return RateQuote(1.0, now)
        cached = self.storage.load_rates()
        quote = self._find(cached, source, target, now)
        if quote is not None:
            return quote
        message = f"Курс {source}→{target} недоступен"
        try:
            refreshed = self.provider()
            self.storage.validate_rates(refreshed)
            # Провайдер мог поставить метку времени позже начала запроса.
            refreshed_now = utils.utc_time(self.clock())
            quote = self._find(refreshed, source, target, refreshed_now)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            raise ApiRequestError(message) from error
        if quote is None:
            raise ApiRequestError(message)
        self.storage.save_rates(refreshed)
        return quote
