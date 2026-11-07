"""JSON Lines и ротация выделенного журнала доменных операций."""

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

from valutatrade_hub.infra.settings import Settings, SettingsLoader

ACTION_LOGGER_NAME = "valutatrade_hub.actions"
_SETUP_LOCK = Lock()
_FIELDS = (
    "action",
    "username",
    "user_id",
    "currency_code",
    "amount",
    "rate",
    "base",
    "result",
    "error_type",
    "error_message",
)


class JsonActionFormatter(logging.Formatter):
    """Только разрешённые поля; полный LogRecord никогда не сериализуется."""

    def format(self, record: logging.LogRecord) -> str:
        action = getattr(record, "action_data", {})
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            **{key: action.get(key) for key in _FIELDS},
        }
        for key in ("before", "after"):
            if key in action:
                data[key] = action[key]
        return json.dumps(data, ensure_ascii=False, allow_nan=False)


class ActionFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # Сбой журнала не отменяет уже сохранённую сделку и не раскрывает traceback.
        print("Не удалось записать журнал действий.", file=sys.stderr)


def setup_logging(settings: Settings | None = None) -> logging.Logger:
    """Настроить один файловый handler; повторный вызов не дублирует записи."""
    configuration = settings if settings is not None else SettingsLoader()
    directory = Path(configuration.get("LOG_DIR", "logs"))
    target = (directory / configuration.get("LOG_FILE", "actions.log")).resolve()
    max_bytes = configuration.get("LOG_MAX_BYTES", 1048576)
    backups = configuration.get("LOG_BACKUP_COUNT", 3)
    with _SETUP_LOCK:
        logger = logging.getLogger(ACTION_LOGGER_NAME)
        logger.setLevel(configuration.get("LOG_LEVEL", "INFO"))
        logger.propagate = False
        if len(logger.handlers) == 1:
            handler = logger.handlers[0]
            if (
                isinstance(handler, ActionFileHandler)
                and handler.baseFilename == str(target)
                and handler.maxBytes == max_bytes
                and handler.backupCount == backups
            ):
                return logger
        directory.mkdir(parents=True, exist_ok=True)
        handler = ActionFileHandler(
            target, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        handler.setFormatter(JsonActionFormatter())
        for previous in logger.handlers[:]:
            logger.removeHandler(previous)
            previous.close()
        logger.addHandler(handler)
        return logger
