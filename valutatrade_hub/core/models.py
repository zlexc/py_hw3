"""Пользователь, кошелёк и портфель без ввода-вывода."""

import hashlib
import hmac
from datetime import datetime

from valutatrade_hub.core import utils
from valutatrade_hub.core.exceptions import InsufficientFundsError


class User:
    """Пользователь с хешем пароля и индивидуальной солью."""

    def __init__(
        self,
        user_id: int,
        username: str,
        hashed_password: str,
        salt: str,
        registration_date: datetime,
    ) -> None:
        self._user_id = utils.user_id(user_id)
        self.username = username
        for value in (hashed_password, salt):
            if not isinstance(value, str) or not value:
                raise ValueError("Хеш пароля и соль должны быть непустыми строками")
        self._hashed_password = hashed_password
        self._salt = salt
        self._registration_date = utils.utc_time(registration_date)

    @property
    def user_id(self) -> int:
        """Вернуть идентификатор владельца без возможности перезаписи."""
        return self._user_id

    @property
    def username(self) -> str:
        """Вернуть имя пользователя с сохранением регистра."""
        return self._username

    @username.setter
    def username(self, value: str) -> None:
        """Проверить непустое имя, убрать внешние пробелы и сохранить регистр."""
        self._username = utils.username(value)

    @property
    def hashed_password(self) -> str:
        """Вернуть сохранённый SHA-256 хеш для сериализации и проверки."""
        return self._hashed_password

    @property
    def salt(self) -> str:
        """Вернуть индивидуальную соль пользователя для сериализации."""
        return self._salt

    @property
    def registration_date(self) -> datetime:
        """Вернуть дату регистрации с часовым поясом UTC."""
        return self._registration_date

    def get_user_info(self) -> dict:
        """Вернуть публичную информацию без хеша и соли."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "registration_date": self.registration_date.isoformat(),
        }

    def change_password(self, new_password: str) -> None:
        """Заменить хеш пароля после проверки минимальной длины в 4 символа."""
        if not isinstance(new_password, str) or len(new_password) < 4:
            raise ValueError("Пароль должен быть не короче 4 символов")
        self._hashed_password = hashlib.sha256(
            (new_password + self.salt).encode("utf-8")
        ).hexdigest()

    def verify_password(self, password: str) -> bool:
        """Сравнить хеш введённого пароля с сохранённым за постоянное время."""
        if not isinstance(password, str):
            return False
        candidate = hashlib.sha256((password + self.salt).encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate.encode(), self.hashed_password.encode())


class Wallet:
    """Баланс одной валюты."""

    def __init__(self, currency_code: str, balance: float = 0.0) -> None:
        self._currency_code = utils.currency_code(currency_code)
        self.balance = balance

    @property
    def currency_code(self) -> str:
        """Вернуть нормализованный код валюты кошелька."""
        return self._currency_code

    @property
    def balance(self) -> float:
        """Вернуть текущий баланс без округления."""
        return self._balance

    @balance.setter
    def balance(self, value: float) -> None:
        """Принять конечное неотрицательное число; bool и другие типы отклонить."""
        self._balance = utils.number(value)

    def deposit(self, amount: float) -> None:
        """Добавить положительную конечную сумму; при ошибке сохранить баланс."""
        self.balance = self.balance + utils.number(amount, positive=True)

    def withdraw(self, amount: float) -> None:
        """Снять сумму; при нехватке вызвать InsufficientFundsError без изменений."""
        amount = utils.number(amount, positive=True)
        if amount > self.balance:
            raise InsufficientFundsError(self.balance, amount, self.currency_code)
        self.balance = self.balance - amount

    def get_balance_info(self) -> dict:
        """Вернуть словарь с кодом валюты и балансом для представления."""
        return {"currency_code": self.currency_code, "balance": self.balance}


class Portfolio:
    """Набор кошельков одного пользователя."""

    def __init__(
        self, user_id: int, user: User, wallets: dict[str, Wallet] | None = None
    ) -> None:
        self._user_id = utils.user_id(user_id)
        if not isinstance(user, User) or user.user_id != self.user_id:
            raise ValueError("Пользователь не соответствует user_id портфеля")
        self._user = user
        if wallets is not None and not isinstance(wallets, dict):
            raise ValueError("Кошельки должны быть словарём")
        self._wallets = {}
        for code, wallet in (wallets or {}).items():
            code = utils.currency_code(code)
            if not isinstance(wallet, Wallet) or code != wallet.currency_code:
                raise ValueError("Код валюты не соответствует кошельку")
            if code in self._wallets:
                raise ValueError(f"Кошелёк '{code}' уже существует")
            self._wallets[code] = wallet

    @property
    def user_id(self) -> int:
        """Вернуть идентификатор владельца без возможности перезаписи."""
        return self._user_id

    @property
    def user(self) -> User:
        """Вернуть владельца портфеля без возможности заменить его свойством."""
        return self._user

    @property
    def wallets(self) -> dict[str, Wallet]:
        """Вернуть поверхностную копию словаря с теми же объектами Wallet."""
        return self._wallets.copy()

    def add_currency(self, currency_code: str) -> Wallet:
        """Создать пустой кошелёк, отклонив повторяющийся код валюты."""
        code = utils.currency_code(currency_code)
        if code in self._wallets:
            raise ValueError(f"Кошелёк '{code}' уже существует")
        wallet = Wallet(code)
        self._wallets[code] = wallet
        return wallet

    def get_wallet(self, currency_code: str) -> Wallet:
        """Вернуть кошелёк по нормализованному коду или сообщить об отсутствии."""
        code = utils.currency_code(currency_code)
        if code not in self._wallets:
            raise ValueError(
                f"У вас нет кошелька '{code}'. Добавьте валюту: "
                "она создаётся автоматически при первой покупке."
            )
        return self._wallets[code]

    def get_total_value(
        self, base_currency: str = "USD", *, exchange_rates: dict | None = None
    ) -> float:
        """Вернуть стоимость в base_currency без чтения файлов или сети.

        exchange_rates задаёт цены единиц валют в одной общей единице измерения.
        Без переданных котировок используются демонстрационные цены в USD.
        Отсутствующие валюты или некорректные котировки вызывают ValueError.
        """
        rates = utils.DEFAULT_USD_RATES if exchange_rates is None else exchange_rates
        base = utils.currency_code(base_currency)
        if base not in rates:
            raise ValueError(f"Неизвестная базовая валюта '{base}'")
        base_rate = utils.number(rates[base], positive=True)
        total = 0.0
        for code, wallet in self._wallets.items():
            if code not in rates:
                raise ValueError(f"Не удалось получить курс для {code}→{base}")
            rate = utils.number(rates[code], positive=True) / base_rate
            total += wallet.balance * rate
        return utils.number(total, label="Стоимость портфеля")
