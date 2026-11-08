"""Доменные ошибки с сообщениями для командного интерфейса."""


class DomainError(ValueError):
    """Ожидаемая ошибка операции валютного кошелька."""


class InsufficientFundsError(DomainError):
    """Доступный баланс меньше запрошенной суммы снятия."""

    def __init__(self, available: float, required: float, code: str) -> None:
        self.available = available
        self.required = required
        self.code = code
        super().__init__(
            f"Недостаточно средств: доступно {available:.4f} {code}, "
            f"требуется {required:.4f} {code}"
        )


class CurrencyNotFoundError(DomainError):
    """Код корректен по формату, но отсутствует в реестре валют."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Неизвестная валюта '{code}'")


class ApiRequestError(DomainError):
    """Безопасное сообщение о недоступности или неверном ответе API."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Ошибка при обращении к внешнему API: {reason}")
