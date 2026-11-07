"""Сценарии приложения; пользовательский ввод и вывод остаются в CLI."""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from valutatrade_hub.core import utils
from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import ApiRequestError, InsufficientFundsError
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.storage import JsonStorage, StorageError
from valutatrade_hub.decorators import log_action
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import DEFAULT_SETTINGS, Settings, SettingsLoader


class WalletService:
    """Регистрация, сессия и операции одного запущенного приложения."""

    def __init__(
        self,
        storage: JsonStorage | DatabaseManager | None = None,
        rates: RateService | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        settings: Settings | None = None,
    ) -> None:
        if settings is None:
            settings = SettingsLoader() if storage is None else DEFAULT_SETTINGS
        self.settings = settings
        self.storage = storage if storage is not None else DatabaseManager(settings)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rates = rates or RateService(
            self.storage, clock=self.clock, settings=self.settings
        )
        self.current_user: User | None = None

    def register(self, username: str, password: str) -> dict:
        with self.storage.operation():
            return self._register(username, password)

    def _register(self, username: str, password: str) -> dict:
        name = utils.username(username)
        users = self.storage.load_users()
        if any(user.username == name for user in users):
            raise ValueError(f"Имя пользователя '{name}' уже занято")
        portfolios = self.storage.load_portfolios(users)
        salt = secrets.token_hex(16)
        existing_salts = {user.salt for user in users}
        while salt in existing_salts:
            salt = secrets.token_hex(16)
        owner_id = max((user.user_id for user in users), default=0) + 1
        user = User(owner_id, name, "initial", salt, self.clock())
        user.change_password(password)
        portfolios[owner_id] = Portfolio(owner_id, user)
        self.storage.save_users([*users, user])
        try:
            self.storage.save_portfolios(portfolios)
        except StorageError:
            # Если второй файл сохранить нельзя, отменить запись пользователя.
            self.storage.save_users(users)
            raise
        return user.get_user_info()

    def login(self, username: str, password: str) -> dict:
        with self.storage.operation():
            return self._login(username, password)

    def _login(self, username: str, password: str) -> dict:
        name = utils.username(username)
        users = self.storage.load_users()
        user = next((user for user in users if user.username == name), None)
        if user is None:
            raise ValueError(f"Пользователь '{name}' не найден")
        if not user.verify_password(password):
            raise ValueError("Неверный пароль")
        self.current_user = user
        return user.get_user_info()

    def _load_portfolio(self) -> tuple[dict[int, Portfolio], Portfolio]:
        if self.current_user is None:
            raise ValueError("Сначала выполните login")
        users = self.storage.load_users()
        user = next(
            (user for user in users if user.user_id == self.current_user.user_id), None
        )
        if user is None:
            raise ValueError("Сначала выполните login")
        portfolios = self.storage.load_portfolios(users)
        if user.user_id not in portfolios:
            portfolios[user.user_id] = Portfolio(user.user_id, user)
        return portfolios, portfolios[user.user_id]

    def show_portfolio(self, base_currency: str | None = None) -> dict:
        with self.storage.operation():
            return self._show_portfolio(base_currency)

    def _show_portfolio(self, base_currency: str | None) -> dict:
        _, portfolio = self._load_portfolio()
        if base_currency is None:
            base_currency = self.settings.get("DEFAULT_BASE_CURRENCY", "USD")
        base = get_currency(base_currency).code
        wallets = portfolio.wallets
        quotes = {"USD": 1.0}
        if wallets:
            for code in {base, *wallets}:
                quotes[code] = self.rates.get_rate(code, "USD").rate
        total = (
            portfolio.get_total_value(base, exchange_rates=quotes) if wallets else 0.0
        )
        return {
            "username": portfolio.user.username,
            "base_currency": base,
            "wallets": [
                {
                    "currency_code": code,
                    "balance": wallet.balance,
                    "value": wallet.balance * (quotes[code] / quotes[base]),
                }
                for code, wallet in sorted(wallets.items())
            ],
            "total": total,
        }

    @log_action("BUY", verbose=True)
    def buy(self, currency_code: str, amount: float) -> dict:
        with self.storage.operation():
            return self._trade("buy", currency_code, amount)

    @log_action("SELL", verbose=True)
    def sell(self, currency_code: str, amount: float) -> dict:
        with self.storage.operation():
            return self._trade("sell", currency_code, amount)

    def _trade(self, operation: str, currency_code: str, amount: float) -> dict:
        portfolios, portfolio = self._load_portfolio()
        code = get_currency(currency_code).code
        amount = utils.number(amount, positive=True)
        wallet = portfolio.wallets.get(code)
        if operation == "sell":
            if wallet is None:
                raise InsufficientFundsError(0.0, amount, code)
            if amount > wallet.balance:
                # Использовать ту же проверку и сообщение, что и в модели.
                wallet.withdraw(amount)
        before = wallet.balance if wallet is not None else 0.0
        quote = self.rates.get_rate(code, "USD")
        estimated_value = utils.number(amount * quote.rate, label="Стоимость сделки")
        if wallet is None:
            wallet = portfolio.add_currency(code)
        if operation == "buy":
            wallet.deposit(amount)
        else:
            wallet.withdraw(amount)
        self.storage.save_portfolios(portfolios)
        return {
            "operation": operation,
            "currency_code": code,
            "amount": amount,
            "rate": quote.rate,
            "before": before,
            "after": wallet.balance,
            "estimated_value": estimated_value,
        }

    def get_rate(self, from_currency: str, to_currency: str) -> dict:
        with self.storage.operation():
            return self._get_rate(from_currency, to_currency)

    def _get_rate(self, from_currency: str, to_currency: str) -> dict:
        source = get_currency(from_currency).code
        target = get_currency(to_currency).code
        quote = self.rates.get_rate(source, target)
        try:
            inverse = utils.number(1 / quote.rate, positive=True)
        except ValueError as error:
            raise ApiRequestError(f"Курс {source}→{target} недоступен") from error
        return {
            "from_currency": source,
            "to_currency": target,
            "rate": quote.rate,
            "inverse_rate": inverse,
            "updated_at": quote.updated_at,
        }
