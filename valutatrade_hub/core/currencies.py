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
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = utils.nonempty_text(value, "Название валюты")

    @property
    def code(self) -> str:
        return self._code

    @code.setter
    def code(self, value: str) -> None:
        normalized = utils.currency_code(value)
        if value != normalized:
            raise ValueError("Код валюты должен быть в верхнем регистре без пробелов")
        self._code = normalized

    @abstractmethod
    def get_display_info(self) -> str:
        """Вернуть описание валюты для интерфейса и логов."""


class FiatCurrency(Currency):
    def __init__(self, name: str, code: str, issuing_country: str) -> None:
        super().__init__(name, code)
        self.issuing_country = issuing_country

    @property
    def issuing_country(self) -> str:
        return self._issuing_country

    @issuing_country.setter
    def issuing_country(self, value: str) -> None:
        self._issuing_country = utils.nonempty_text(value, "Страна или зона эмиссии")

    def get_display_info(self) -> str:
        return f"[FIAT] {self.code} — {self.name} (Issuing: {self.issuing_country})"


class CryptoCurrency(Currency):
    def __init__(self, name: str, code: str, algorithm: str, market_cap: float) -> None:
        super().__init__(name, code)
        self.algorithm = algorithm
        self.market_cap = market_cap

    @property
    def algorithm(self) -> str:
        return self._algorithm

    @algorithm.setter
    def algorithm(self, value: str) -> None:
        self._algorithm = utils.nonempty_text(value, "Алгоритм")

    @property
    def market_cap(self) -> float:
        return self._market_cap

    @market_cap.setter
    def market_cap(self, value: float) -> None:
        self._market_cap = utils.number(value, label="Капитализация")

    def get_display_info(self) -> str:
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
    return tuple(sorted(_REGISTRY))


def get_currency(code: str) -> Currency:
    """Нормализовать ввод и создать независимый экземпляр известной валюты."""
    normalized = utils.currency_code(code)
    if normalized not in _REGISTRY:
        raise CurrencyNotFoundError(normalized)
    return _REGISTRY[normalized]()
