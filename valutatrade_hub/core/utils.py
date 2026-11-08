"""Общие проверки значений и времени."""

import math
import re
from datetime import UTC, datetime

DEFAULT_USD_RATES = {
    "USD": 1.0,
    "EUR": 1.0786,
    "GBP": 1.25,
    "BTC": 59337.21,
    "RUB": 0.01016,
    "ETH": 3720.0,
    "SOL": 145.12,
}

MAX_INTERVAL_SECONDS = 365 * 24 * 60 * 60


def positive_interval(value: float, label: str) -> float:
    """Проверить конечный положительный интервал не длиннее одного года."""
    try:
        result = number(value, positive=True)
    except ValueError:
        raise ValueError(f"{label} должен быть положительным конечным числом") from None
    if result > MAX_INTERVAL_SECONDS:
        raise ValueError(f"{label} не должен превышать один год")
    return result


def currency_code(value: str) -> str:
    """Нормализовать непустой буквенно-цифровой код валюты."""
    if not isinstance(value, str):
        raise ValueError("Код валюты должен быть непустой строкой")
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,4}", normalized):
        raise ValueError("Код валюты: 2–5 латинских букв или цифр, первая — буква")
    return normalized


def nonempty_text(value: str, label: str) -> str:
    """Проверить непустой текст и удалить внешние пробелы."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} не может быть пустым")
    return value.strip()


def number(value: float, *, positive: bool = False, label: str = "Баланс") -> float:
    """Принять только конечное неотрицательное (либо положительное) число."""
    message = (
        "'amount' должен быть положительным числом"
        if positive
        else f"{label} должен быть неотрицательным конечным числом"
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(message) from None
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(message)
    return result


def username(value: str) -> str:
    """Убрать внешние пробелы, сохранив регистр имени."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Имя пользователя не может быть пустым")
    return value.strip()


def user_id(value: int) -> int:
    """Проверить положительный целочисленный идентификатор."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("user_id должен быть положительным целым числом")
    return value


def utc_time(value: datetime) -> datetime:
    """Считать время без часового пояса UTC; остальные значения привести к UTC."""
    if not isinstance(value, datetime):
        raise ValueError("Дата должна быть объектом datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_time(value: str) -> datetime:
    """Прочитать дату ISO 8601, включая примеры без часового пояса."""
    if not isinstance(value, str):
        raise ValueError("Дата должна быть строкой ISO 8601")
    return utc_time(datetime.fromisoformat(value))
