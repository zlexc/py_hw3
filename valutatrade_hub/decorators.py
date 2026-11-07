"""Декоратор журналирования операций без изменения их результата."""

import inspect
import logging
import math
from functools import wraps

from valutatrade_hub.core import utils
from valutatrade_hub.logging_config import ACTION_LOGGER_NAME


def _safe_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _emit(data: dict) -> None:
    try:
        logging.getLogger(ACTION_LOGGER_NAME).info("", extra={"action_data": data})
    except Exception:
        # Ошибка стороннего handler не подменяет результат доменной операции.
        pass


def log_action(action: str, *, verbose: bool = False):
    """Логировать только whitelist полей; исключения операции пробрасывать дальше."""

    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def wrapper(self, *args, **kwargs):
            try:
                arguments = signature.bind_partial(self, *args, **kwargs).arguments
            except TypeError:
                arguments = {}
            user = self.current_user
            try:
                code = utils.currency_code(arguments.get("currency_code"))
            except ValueError:
                code = None
            data = {
                "action": action,
                "username": user.username if user is not None else None,
                "user_id": user.user_id if user is not None else None,
                "currency_code": code,
                "amount": _safe_number(arguments.get("amount")),
                "rate": None,
                "base": "USD",
            }
            try:
                result = function(self, *args, **kwargs)
            except Exception as error:
                _emit(
                    {
                        **data,
                        "result": "ERROR",
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                    }
                )
                raise
            data.update(result="OK", rate=_safe_number(result.get("rate")))
            if verbose:
                data.update(
                    before=_safe_number(result.get("before")),
                    after=_safe_number(result.get("after")),
                )
            _emit(data)
            return result

        return wrapper

    return decorate
