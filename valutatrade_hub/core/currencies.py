"""Иерархия валют и фабрика поддерживаемых кодов."""

from abc import ABC, abstractmethod
from functools import partial

from valutatrade_hub.core import utils
from valutatrade_hub.core.exceptions import CurrencyNotFoundError


class Currency(ABC):
    """Общий интерфейс валюты с проверяемыми публичными свойствами."""

    def __init__(self, name: str, code: str) -> None:
        self.name = name
        self.code = code

    @property
    def name(self) -> str:
        """Вернуть человекочитаемое название валюты."""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        """Проверить непустое название и сохранить его без внешних пробелов."""
        self._name = utils.nonempty_text(value, "Название валюты")

    @property
    def code(self) -> str:
        """Вернуть код валюты в верхнем регистре."""
        return self._code

    @code.setter
    def code(self, value: str) -> None:
        """Сохранить только код из 2–5 символов в верхнем регистре без пробелов."""
        normalized = utils.currency_code(value)
        if value != normalized:
            raise ValueError("Код валюты должен быть в верхнем регистре без пробелов")
        self._code = normalized

    @abstractmethod
    def get_display_info(self) -> str:
        """Вернуть описание валюты для интерфейса и логов."""


class FiatCurrency(Currency):
    """Фиатная валюта с названием страны или зоны эмиссии."""

    def __init__(self, name: str, code: str, issuing_country: str) -> None:
        super().__init__(name, code)
        self.issuing_country = issuing_country

    @property
    def issuing_country(self) -> str:
        """Вернуть страну или зону эмиссии фиатной валюты."""
        return self._issuing_country

    @issuing_country.setter
    def issuing_country(self, value: str) -> None:
        """Сохранить непустую страну или зону эмиссии."""
        self._issuing_country = utils.nonempty_text(value, "Страна или зона эмиссии")

    def get_display_info(self) -> str:
        """Вернуть описание валюты с метаданными её конкретного типа."""
        return f"[FIAT] {self.code} — {self.name} (Issuing: {self.issuing_country})"


class CryptoCurrency(Currency):
    """Криптовалюта с алгоритмом и демонстрационной капитализацией."""

    def __init__(self, name: str, code: str, algorithm: str, market_cap: float) -> None:
        super().__init__(name, code)
        self.algorithm = algorithm
        self.market_cap = market_cap

    @property
    def algorithm(self) -> str:
        """Вернуть название алгоритма криптовалюты."""
        return self._algorithm

    @algorithm.setter
    def algorithm(self, value: str) -> None:
        """Сохранить непустое название алгоритма."""
        self._algorithm = utils.nonempty_text(value, "Алгоритм")

    @property
    def market_cap(self) -> float:
        """Вернуть последнюю заданную капитализацию криптовалюты."""
        return self._market_cap

    @market_cap.setter
    def market_cap(self, value: float) -> None:
        """Принять конечную неотрицательную капитализацию, исключая bool."""
        self._market_cap = utils.number(value, label="Капитализация")

    def get_display_info(self) -> str:
        """Вернуть описание валюты с метаданными её конкретного типа."""
        mantissa, exponent = f"{self.market_cap:.2e}".split("e")
        capitalization = f"{mantissa}e{int(exponent)}"
        return (
            f"[CRYPTO] {self.code} — {self.name} "
            f"(Algo: {self.algorithm}, MCAP: {capitalization})"
        )


# Метаданные криптовалют демонстрационные, не обновляются из внешних API.
_REGISTRY = {
    "USD": partial(FiatCurrency, "US Dollar", "USD", "United States"),
    "EUR": partial(FiatCurrency, "Euro", "EUR", "Eurozone"),
    "RUB": partial(FiatCurrency, "Russian Ruble", "RUB", "Russia"),
    "GBP": partial(FiatCurrency, "British Pound", "GBP", "United Kingdom"),
    "BTC": partial(CryptoCurrency, "Bitcoin", "BTC", "SHA-256", 1.12e12),
    "ETH": partial(CryptoCurrency, "Ethereum", "ETH", "Ethash", 4.5e11),
    "SOL": partial(CryptoCurrency, "Solana", "SOL", "Proof of History", 7.0e10),
}


def supported_codes() -> tuple[str, ...]:
    """Вернуть отсортированные коды всех зарегистрированных валют."""
    return tuple(sorted(_REGISTRY))


def get_currency(code: str) -> Currency:
    """Нормализовать ввод и создать независимый экземпляр известной валюты."""
    normalized = utils.currency_code(code)
    if normalized not in _REGISTRY:
        raise CurrencyNotFoundError(normalized)
    return _REGISTRY[normalized]()


def currency_source(code: str) -> str:
    """Выбрать источник котировок: CoinGecko для криптовалют, ExchangeRate для фиата."""
    return (
        "coingecko"
        if isinstance(get_currency(code), CryptoCurrency)
        else "exchangerate"
    )
