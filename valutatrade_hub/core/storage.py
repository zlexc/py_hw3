"""Проверяемое JSON-хранилище с атомарной заменой отдельных файлов."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from valutatrade_hub.core import utils
from valutatrade_hub.core.models import Portfolio, User, Wallet


class StorageError(ValueError):
    """Данные нельзя безопасно прочитать или сохранить."""


def read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        raise StorageError(f"Не удалось прочитать {path.name}: {error}") from error


def write_json(path: Path, data) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    except (OSError, ValueError) as error:
        raise StorageError(f"Не удалось сохранить {path.name}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def rates_lock(data_dir: Path):
    """Общий POSIX lock для CLI, инициализации кеша и отдельного парсера."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / ".rates.lock").open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as error:
        raise StorageError("Не удалось заблокировать кеш курсов") from error


def normalize_rates(data: dict) -> dict:
    """Проверить новый или старый формат и вернуть канонический снимок."""
    if not isinstance(data, dict):
        raise ValueError("ожидается объект курсов")
    if "pairs" in data:
        if set(data) - {"pairs", "last_refresh"}:
            raise ValueError("неизвестные поля снимка курсов")
        pairs = data["pairs"]
        if not isinstance(pairs, dict):
            raise ValueError("pairs должен быть объектом")
    else:
        pairs = {
            key: value
            for key, value in data.items()
            if key not in ("source", "last_refresh")
        }
    source = data.get("source", "Legacy")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source должен быть непустой строкой")
    normalized = {}
    for pair, entry in pairs.items():
        if not isinstance(pair, str):
            raise ValueError("ключ курса должен быть строкой")
        parts = pair.split("_")
        if len(parts) != 2 or any(code != utils.currency_code(code) for code in parts):
            raise ValueError("ожидается пара вида EUR_USD")
        if not isinstance(entry, dict):
            raise ValueError("курс должен быть объектом")
        rate = utils.number(entry["rate"], positive=True)
        timestamp = utils.parse_time(entry["updated_at"]).isoformat()
        entry_source = utils.nonempty_text(entry.get("source", source), "source")
        normalized[pair] = {
            "rate": rate,
            "updated_at": timestamp,
            "source": entry_source,
        }
    last_refresh = data.get("last_refresh")
    if last_refresh is not None:
        last_refresh = utils.parse_time(last_refresh).isoformat()
    elif normalized:
        last_refresh = max(
            utils.parse_time(entry["updated_at"]) for entry in normalized.values()
        ).isoformat()
    return {"pairs": normalized, "last_refresh": last_refresh}


class JsonStorage:
    """Файлы пользователей, портфелей и последних курсов."""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self._operation_lock = RLock()
        # Проверить существующие файлы до создания недостающих.
        users = self.load_users()
        self.load_portfolios(users)
        self.load_rates()
        for filename, default in (
            ("users.json", []),
            ("portfolios.json", []),
        ):
            if not (self.data_dir / filename).exists():
                self._write(filename, default)
        with rates_lock(self.data_dir):
            if not (self.data_dir / "rates.json").exists():
                self._write("rates.json", {"pairs": {}, "last_refresh": None})

    @contextmanager
    def operation(self):
        """Сериализовать сценарии, использующие один экземпляр хранилища."""
        with self._operation_lock:
            yield self

    def _read(self, filename: str, default):
        return read_json(self.data_dir / filename, default)

    def _write(self, filename: str, data) -> None:
        write_json(self.data_dir / filename, data)

    def load_users(self) -> list[User]:
        data = self._read("users.json", [])
        try:
            if not isinstance(data, list):
                raise ValueError("ожидается список пользователей")
            users = []
            ids, names = set(), set()
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("пользователь должен быть объектом")
                user = User(
                    item["user_id"],
                    item["username"],
                    item["hashed_password"],
                    item["salt"],
                    utils.parse_time(item["registration_date"]),
                )
                if user.user_id in ids or user.username in names:
                    raise ValueError("повторяющийся идентификатор или имя пользователя")
                ids.add(user.user_id)
                names.add(user.username)
                users.append(user)
            return users
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError(f"Некорректная структура users.json: {error}") from error

    def save_users(self, users: list[User]) -> None:
        self._write(
            "users.json",
            [
                {
                    **user.get_user_info(),
                    "hashed_password": user.hashed_password,
                    "salt": user.salt,
                }
                for user in users
            ],
        )

    def load_portfolios(self, users: list[User]) -> dict[int, Portfolio]:
        data = self._read("portfolios.json", [])
        try:
            if not isinstance(data, list):
                raise ValueError("ожидается список портфелей")
            by_id = {user.user_id: user for user in users}
            portfolios = {}
            for item in data:
                if not isinstance(item, dict) or not isinstance(item["wallets"], dict):
                    raise ValueError("ожидается объект портфеля со словарём wallets")
                owner_id = utils.user_id(item["user_id"])
                if owner_id not in by_id or owner_id in portfolios:
                    raise ValueError("неизвестный или повторяющийся user_id портфеля")
                wallets = {}
                for code, entry in item["wallets"].items():
                    if not isinstance(entry, dict) or code != utils.currency_code(code):
                        raise ValueError("некорректный кошелёк или код валюты")
                    if entry.get("currency_code", code) != code:
                        raise ValueError("код валюты не соответствует ключу кошелька")
                    wallets[code] = Wallet(code, entry["balance"])
                portfolios[owner_id] = Portfolio(owner_id, by_id[owner_id], wallets)
            return portfolios
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError(
                f"Некорректная структура portfolios.json: {error}"
            ) from error

    def save_portfolios(self, portfolios: dict[int, Portfolio]) -> None:
        self._write(
            "portfolios.json",
            [
                {
                    "user_id": portfolio.user_id,
                    "wallets": {
                        code: {"balance": wallet.balance}
                        for code, wallet in portfolio.wallets.items()
                    },
                }
                for portfolio in portfolios.values()
            ],
        )

    @staticmethod
    def validate_rates(data: dict) -> None:
        normalize_rates(data)

    def load_rates(self) -> dict:
        data = self._read("rates.json", {})
        try:
            return normalize_rates(data)
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError(f"Некорректная структура rates.json: {error}") from error

    def save_rates(self, data: dict) -> None:
        try:
            data = normalize_rates(data)
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError(f"Некорректные данные курсов: {error}") from error
        with rates_lock(self.data_dir):
            self._write("rates.json", data)
