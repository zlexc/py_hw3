"""История и публикация снимка; пользователи и портфели парсеру не нужны."""

from pathlib import Path

from valutatrade_hub.core import utils
from valutatrade_hub.core.storage import (
    StorageError,
    normalize_rates,
    rates_lock,
    read_json,
    write_json,
)
from valutatrade_hub.parser_service.api_clients import iso_utc


class ParserStorage:
    """Валидированная история и последний снимок с общим файловым lock."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)

    def load_cache(self) -> dict:
        """Прочитать последний снимок без создания отсутствующего файла."""
        try:
            return normalize_rates(read_json(self.data_dir / "rates.json", {}))
        except (KeyError, ValueError, TypeError) as error:
            if isinstance(error, StorageError):
                raise
            raise StorageError("Некорректная структура rates.json") from error

    def load_history(self) -> list[dict]:
        """Прочитать историю и проверить структуру и уникальность идентификаторов."""
        history = read_json(self.data_dir / "exchange_rates.json", [])
        try:
            if not isinstance(history, list):
                raise ValueError
            ids = set()
            for entry in history:
                self.validate_measurement(entry)
                if entry["id"] in ids:
                    raise ValueError("повторяющийся id")
                ids.add(entry["id"])
        except (KeyError, ValueError, TypeError) as error:
            raise StorageError("Некорректная структура exchange_rates.json") from error
        return history

    @staticmethod
    def validate_measurement(entry: dict) -> None:
        """Проверить коды валют, курс, источник и согласованность ID со временем."""
        if not isinstance(entry, dict):
            raise ValueError("измерение должно быть объектом")
        for key in ("from_currency", "to_currency"):
            if entry[key] != utils.currency_code(entry[key]):
                raise ValueError("некорректный код валюты")
        utils.number(entry["rate"], positive=True)
        timestamp = utils.parse_time(entry["timestamp"])
        expected = (
            f"{entry['from_currency']}_{entry['to_currency']}_{iso_utc(timestamp)}"
        )
        # Примеры без микросекунд допустимы при чтении старой истории.
        short = expected.replace(".000000Z", "Z")
        if entry["id"] not in (expected, short):
            raise ValueError("id не соответствует паре и времени")
        utils.nonempty_text(entry["source"], "source")
        if not isinstance(entry["meta"], dict):
            raise ValueError("meta должен быть объектом")

    def commit(
        self, measurements: list[dict], completed_at
    ) -> tuple[int, int, str | None]:
        """Под lock добавить историю и новые пары; вернуть counts и last_refresh."""
        for entry in measurements:
            self.validate_measurement(entry)
        with rates_lock(self.data_dir):
            cache = self.load_cache()
            history = self.load_history()
            known = {entry["id"] for entry in history}
            added = 0
            changed = set()
            for entry in measurements:
                if entry["id"] not in known:
                    history.append(entry)
                    known.add(entry["id"])
                    added += 1
                pair = f"{entry['from_currency']}_{entry['to_currency']}"
                current = cache["pairs"].get(pair)
                if current is None or utils.parse_time(
                    entry["timestamp"]
                ) > utils.parse_time(current["updated_at"]):
                    cache["pairs"][pair] = {
                        "rate": entry["rate"],
                        "updated_at": entry["timestamp"],
                        "source": entry["source"],
                    }
                    changed.add(pair)
            if changed:
                previous = cache["last_refresh"]
                refreshed = max(
                    utils.utc_time(completed_at),
                    *(utils.parse_time(entry["timestamp"]) for entry in measurements),
                )
                if previous is not None:
                    refreshed = max(refreshed, utils.parse_time(previous))
                cache["last_refresh"] = iso_utc(refreshed)
            if added:
                write_json(self.data_dir / "exchange_rates.json", history)
            if changed:
                write_json(self.data_dir / "rates.json", cache)
            return len(changed), added, cache["last_refresh"]
