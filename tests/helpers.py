"""Изолированное окружение и фиксированное время для тестов."""

from datetime import UTC, datetime

from valutatrade_hub.core.models import User

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def make_user(user_id=1, username="alice", password="1234", salt="test-salt"):
    user = User(user_id, username, "initial", salt, NOW)
    user.change_password(password)
    return user
