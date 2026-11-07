"""Однократная загрузка необязательного config.json."""

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any

from valutatrade_hub.core import utils
from valutatrade_hub.core.currencies import get_currency

DEFAULT_SETTINGS = MappingProxyType(
    {
        "DATA_DIR": "data",
        "RATES_TTL_SECONDS": 300,
        "DEFAULT_BASE_CURRENCY": "USD",
        "LOG_DIR": "logs",
        "LOG_FILE": "actions.log",
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
        "LOG_MAX_BYTES": 1048576,
        "LOG_BACKUP_COUNT": 3,
    }
)


class SettingsLoader:
    """Один снимок настроек на процесс; импорты не читают конфигурацию."""

    _instance = None
    _instance_lock = Lock()

    def __new__(cls, config_path: str | Path | None = None):
        # __new__ проще метакласса; и создание, и загрузка защищены одним lock.
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._load(config_path)
                cls._instance = instance
            elif config_path is not None:
                if Path(config_path).resolve() != cls._instance._config_path:
                    raise ValueError("SettingsLoader уже загружен из другого файла")
            return cls._instance

    def __init__(self, config_path: str | Path | None = None) -> None:
        # Инициализация в __new__ не повторяется при последующих вызовах.
        pass

    def _load(self, config_path: str | Path | None) -> None:
        self._config_path = Path(config_path or "config.json").resolve()
        try:
            with self._config_path.open(encoding="utf-8") as stream:
                overrides = json.load(stream)
        except FileNotFoundError:
            overrides = {}
        except (OSError, ValueError) as error:
            raise ValueError("Не удалось прочитать конфигурацию config.json") from error
        if not isinstance(overrides, dict):
            raise ValueError("Конфигурация должна быть JSON-объектом")
        values = {**DEFAULT_SETTINGS, **overrides}
        for key in ("DATA_DIR", "LOG_DIR"):
            directory = utils.nonempty_text(values[key], key)
            values[key] = Path(directory).resolve()
        filename = utils.nonempty_text(values["LOG_FILE"], "LOG_FILE")
        if Path(filename).name != filename or filename in (".", ".."):
            raise ValueError("LOG_FILE должен быть именем файла без пути")
        values["LOG_FILE"] = filename
        try:
            values["RATES_TTL_SECONDS"] = utils.number(
                values["RATES_TTL_SECONDS"], positive=True
            )
            timedelta(seconds=values["RATES_TTL_SECONDS"])
        except (ValueError, OverflowError):
            raise ValueError(
                "RATES_TTL_SECONDS должен быть положительным числом "
                "в диапазоне timedelta"
            ) from None
        for key, minimum in (("LOG_MAX_BYTES", 1), ("LOG_BACKUP_COUNT", 0)):
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{key} должен быть целым числом от {minimum}")
        values["DEFAULT_BASE_CURRENCY"] = get_currency(
            values["DEFAULT_BASE_CURRENCY"]
        ).code
        if values["LOG_FORMAT"] != "json":
            raise ValueError("LOG_FORMAT должен быть 'json'")
        if values["LOG_LEVEL"] not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("LOG_LEVEL: DEBUG, INFO, WARNING, ERROR или CRITICAL")
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        """Вернуть значение, не позволяя изменить кеш через вложенный объект."""
        if key not in self._values:
            return default
        return deepcopy(self._values[key])


Settings = SettingsLoader | Mapping[str, Any]
