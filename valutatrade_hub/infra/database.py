"""Singleton-адаптер существующего JSON-хранилища."""

from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.storage import JsonStorage
from valutatrade_hub.infra.settings import Settings, SettingsLoader


class DatabaseManager:
    """Один доступ к данным с блокировкой операций внутри процесса."""

    _instance = None
    _instance_lock = Lock()

    def __new__(cls, settings: Settings | None = None):
        """Получить единственный экземпляр, отклонив смену каталога данных."""
        # __new__ наглядно гарантирует единственный полностью готовый экземпляр.
        with cls._instance_lock:
            if cls._instance is None:
                configuration = settings if settings is not None else SettingsLoader()
                instance = super().__new__(cls)
                instance._storage = JsonStorage(configuration.get("DATA_DIR", "data"))
                cls._instance = instance
            elif settings is not None:
                requested = Path(settings.get("DATA_DIR", "data")).resolve()
                if requested != cls._instance.data_dir.resolve():
                    raise ValueError("DatabaseManager уже использует другой каталог")
            return cls._instance

    def __init__(self, settings: Settings | None = None) -> None:
        pass

    @property
    def data_dir(self) -> Path:
        """Вернуть каталог JSON-файлов общего хранилища."""
        return self._storage.data_dir

    @contextmanager
    def operation(self):
        """Удержать общий reentrant lock на весь read→modify→write сценарий."""
        with self._storage.operation():
            yield self

    def load_users(self) -> list[User]:
        """Прочитать и проверить пользователей, уникальность имён и идентификаторов."""
        with self.operation():
            return self._storage.load_users()

    def save_users(self, users: list[User]) -> None:
        """Атомарно сохранить пользователей с хешами, солью и ISO-датами."""
        with self.operation():
            self._storage.save_users(users)

    def load_portfolios(self, users: list[User]) -> dict[int, Portfolio]:
        """Восстановить портфели и проверить владельцев и балансы кошельков."""
        with self.operation():
            return self._storage.load_portfolios(users)

    def save_portfolios(self, portfolios: dict[int, Portfolio]) -> None:
        """Атомарно сохранить словарь портфелей в принятом JSON-формате."""
        with self.operation():
            self._storage.save_portfolios(portfolios)

    def load_rates(self) -> dict:
        """Прочитать кеш и вернуть канонический снимок с pairs и last_refresh."""
        with self.operation():
            return self._storage.load_rates()

    def save_rates(self, data: dict) -> None:
        """Проверить и сохранить канонический кеш под файловой блокировкой."""
        with self.operation():
            self._storage.save_rates(data)

    validate_rates = staticmethod(JsonStorage.validate_rates)
