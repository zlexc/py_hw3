"""Изолированное окружение и фиксированное время для тестов."""

from datetime import UTC, datetime

from valutatrade_hub.core.models import User
from valutatrade_hub.core.rates import RateService

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def make_user(user_id=1, username="alice", password="1234", salt="test-salt"):
    user = User(user_id, username, "initial", salt, NOW)
    user.change_password(password)
    return user


def stub_rates(clock=lambda: NOW):
    """Явная тестовая заглушка; production её не импортирует."""
    values = {
        "EUR": 1.0786,
        "GBP": 1.25,
        "RUB": 0.01016,
        "BTC": 59337.21,
        "ETH": 3720.0,
        "SOL": 145.12,
    }
    return {
        "pairs": {
            f"{code}_USD": {
                "rate": rate,
                "updated_at": clock().isoformat(),
                "source": "TestStub",
            }
            for code, rate in values.items()
        },
        "last_refresh": clock().isoformat(),
    }


def make_rates(storage, *, clock=lambda: NOW, settings=None):
    return RateService(
        storage, provider=lambda: stub_rates(clock), clock=clock, settings=settings
    )
