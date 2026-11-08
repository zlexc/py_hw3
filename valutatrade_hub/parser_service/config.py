"""Проверяемая конфигурация парсера и локальные ключи без побочных эффектов."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from valutatrade_hub.core import utils
from valutatrade_hub.core.currencies import CryptoCurrency, FiatCurrency, get_currency
from valutatrade_hub.infra.settings import Settings


def local_secrets(path: Path) -> dict[str, str]:
    """Прочитать только ключи API; не выполнять shell-код из .env."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError("Не удалось прочитать локальный .env") from None
    result = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if key.strip() not in ("EXCHANGERATE_API_KEY", "COINGECKO_API_KEY"):
            continue
        if not separator:
            raise ValueError("В .env ожидаются строки KEY=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


@dataclass(frozen=True)
class ParserConfig:
    data_dir: Path = field(default_factory=lambda: Path("data").resolve())
    base_currency: str = "USD"
    fiat_currencies: tuple[str, ...] = ("EUR", "GBP", "RUB")
    crypto_currencies: tuple[str, ...] = ("BTC", "ETH", "SOL")
    crypto_id_map: dict[str, str] = field(
        default_factory=lambda: {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
    )
    coingecko_url: str = "https://api.coingecko.com/api/v3/simple/price"
    exchangerate_api_url: str = "https://v6.exchangerate-api.com/v6"
    request_timeout: float = 10
    update_interval_seconds: float = 3600
    exchangerate_api_key: str = field(default="", repr=False)
    coingecko_api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        if self.base_currency != "USD":
            raise ValueError("Parser Service поддерживает только BASE_CURRENCY=USD")
        for name, expected in (
            ("fiat_currencies", FiatCurrency),
            ("crypto_currencies", CryptoCurrency),
        ):
            codes = getattr(self, name)
            if not isinstance(codes, (tuple, list)):
                raise ValueError(f"{name} должен быть списком кодов валют")
            normalized = tuple(get_currency(code).code for code in codes)
            if len(set(normalized)) != len(normalized) or "USD" in normalized:
                raise ValueError(f"{name}: повторяющийся код или USD")
            if any(not isinstance(get_currency(code), expected) for code in normalized):
                raise ValueError(f"Неверный тип валюты в {name}")
            object.__setattr__(self, name, normalized)
        if not self.fiat_currencies and not self.crypto_currencies:
            raise ValueError("Нужно настроить хотя бы одну валюту парсера")
        if not isinstance(self.crypto_id_map, dict):
            raise ValueError("CRYPTO_ID_MAP должен быть словарём")
        mapping = {}
        for code in self.crypto_currencies:
            raw_id = self.crypto_id_map.get(code)
            mapping[code] = utils.nonempty_text(raw_id, "CoinGecko ID")
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("CoinGecko ID не должны повторяться")
        object.__setattr__(self, "crypto_id_map", mapping)
        for name in ("request_timeout", "update_interval_seconds"):
            try:
                value = utils.number(getattr(self, name), positive=True)
            except ValueError:
                raise ValueError(f"{name} должен быть положительным числом") from None
            if value > 86400 * 365:
                raise ValueError(f"{name} не должен превышать один год")
            object.__setattr__(self, name, value)
        for name in ("coingecko_url", "exchangerate_api_url"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name}: требуется HTTPS URL")
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{name}: требуется HTTPS URL без ключей и параметров")
        for key in (self.exchangerate_api_key, self.coingecko_api_key):
            if not isinstance(key, str) or any(char.isspace() for char in key):
                raise ValueError("Ключ API должен быть строкой без пробелов")

    @property
    def rates_file_path(self) -> Path:
        return self.data_dir / "rates.json"

    @property
    def history_file_path(self) -> Path:
        return self.data_dir / "exchange_rates.json"

    @classmethod
    def from_settings(cls, settings: Settings, *, env_path: Path | None = None):
        values = settings.get("PARSER", {})
        if not isinstance(values, dict):
            raise ValueError("PARSER должен быть JSON-объектом")
        names = {
            "BASE_CURRENCY": "base_currency",
            "FIAT_CURRENCIES": "fiat_currencies",
            "CRYPTO_CURRENCIES": "crypto_currencies",
            "CRYPTO_ID_MAP": "crypto_id_map",
            "COINGECKO_URL": "coingecko_url",
            "EXCHANGERATE_API_URL": "exchangerate_api_url",
            "REQUEST_TIMEOUT": "request_timeout",
            "UPDATE_INTERVAL_SECONDS": "update_interval_seconds",
        }
        if set(values) - names.keys():
            raise ValueError("Неизвестные настройки PARSER; ключи API задаются в .env")
        secrets = local_secrets(env_path if env_path is not None else Path(".env"))
        return cls(
            data_dir=settings.get("DATA_DIR", "data"),
            **{names[name]: value for name, value in values.items()},
            exchangerate_api_key=os.environ.get(
                "EXCHANGERATE_API_KEY", secrets.get("EXCHANGERATE_API_KEY", "")
            ),
            coingecko_api_key=os.environ.get(
                "COINGECKO_API_KEY", secrets.get("COINGECKO_API_KEY", "")
            ),
        )
