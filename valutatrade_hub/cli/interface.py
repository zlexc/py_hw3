"""Единственный обработчик пользовательских команд."""

import argparse
import shlex
import sys
from collections.abc import Callable
from functools import partial
from typing import TextIO

from prettytable import PrettyTable

from valutatrade_hub.core.currencies import get_currency, supported_codes
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
)
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import setup_logging, setup_parser_logging


class CommandParser(argparse.ArgumentParser):
    """Ошибки аргументов не завершают REPL и не раскрывают введённый пароль."""

    def __init__(self, *args, output: TextIO, **kwargs) -> None:
        self.output = output
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise ValueError("Некорректные аргументы команды. Выполните help.")

    def _print_message(self, message, file=None) -> None:
        if message:
            self.output.write(message)


def build_parser(output: TextIO) -> CommandParser:
    parser = CommandParser(
        prog="", description="Команды валютного кошелька", output=output
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=partial(CommandParser, output=output),
    )
    for name, description in (
        ("register", "зарегистрироваться"),
        ("login", "войти в систему"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--username", required=True)
        command.add_argument("--password", required=True)
    command = commands.add_parser("show-portfolio", help="показать портфель")
    command.add_argument(
        "--base", default=None, help="база из конфигурации, обычно USD"
    )
    for name, description in (("buy", "купить валюту"), ("sell", "продать валюту")):
        command = commands.add_parser(name, help=description)
        command.add_argument("--currency", required=True)
        command.add_argument("--amount", required=True)
    command = commands.add_parser("get-rate", help="получить курс валюты")
    command.add_argument("--from", dest="from_currency", required=True)
    command.add_argument("--to", dest="to_currency", required=True)
    command = commands.add_parser("update-rates", help="обновить курсы из API")
    command.add_argument("--source", choices=("coingecko", "exchangerate"))
    command = commands.add_parser("show-rates", help="показать локальный кеш курсов")
    command.add_argument("--currency")
    command.add_argument("--top", type=int)
    command.add_argument("--base")
    commands.add_parser("help", help="показать справку")
    commands.add_parser("exit", help="завершить программу")
    return parser


class WalletApplication:
    """REPL с одной сессией и выделенным сервисом бизнес-логики."""

    def __init__(
        self,
        service: WalletService,
        *,
        input_fn: Callable[[], str] | None = None,
        output: TextIO | None = None,
    ) -> None:
        self.service = service
        self.input_fn = input_fn or input
        self.output = output if output is not None else sys.stdout
        self.parser = build_parser(self.output)

    def _print(self, message: str = "") -> None:
        print(message, file=self.output)

    def run(self) -> None:
        self._print("ValutaTrade Hub — валютный кошелёк. Справка: help. Выход: exit.")
        while True:
            try:
                print("> ", end="", file=self.output, flush=True)
                line = self.input_fn()
                if not self.handle_line(line):
                    return
            except (EOFError, KeyboardInterrupt):
                self._print("\nДо свидания!")
                return
            except InsufficientFundsError as error:
                self._print(str(error))
            except CurrencyNotFoundError as error:
                self._print(str(error))
                self._print(f"Поддерживаемые валюты: {', '.join(supported_codes())}")
                self._print("Справка: get-rate --help")
            except ApiRequestError as error:
                self._print(str(error))
                self._print("Повторите попытку позже или проверьте подключение к сети.")
                self._print("Для обновления курсов: update-rates")
            except (ValueError, OSError) as error:
                self._print(f"Ошибка: {error}")

    def handle_line(self, line: str) -> bool:
        try:
            arguments = shlex.split(line)
        except ValueError:
            raise ValueError(
                "Не удалось разобрать команду: проверьте кавычки"
            ) from None
        if not arguments:
            return True
        try:
            args = self.parser.parse_args(arguments)
        except SystemExit as error:
            if error.code == 0:  # argparse --help
                return True
            raise
        match args.command:
            case "exit":
                self._print("До свидания!")
                return False
            case "help":
                self.parser.print_help(file=self.output)
                self._print("Подробности команды: <команда> --help")
                self._print("Поддерживаемые валюты (метаданные демонстрационные):")
                for code in supported_codes():
                    self._print(get_currency(code).get_display_info())
            case "register":
                result = self.service.register(args.username, args.password)
                self._print(
                    f"Пользователь '{result['username']}' зарегистрирован "
                    f"(id={result['user_id']}). Войдите: login "
                    f"--username {shlex.quote(result['username'])} --password ****"
                )
            case "login":
                result = self.service.login(args.username, args.password)
                self._print(f"Вы вошли как '{result['username']}'")
            case "show-portfolio":
                self._show_portfolio(self.service.show_portfolio(args.base))
            case "buy" | "sell":
                try:
                    amount = float(args.amount)
                except (ValueError, OverflowError):
                    # Валидация и журналирование отказа выполняются в usecase.
                    amount = args.amount
                operation = (
                    self.service.buy if args.command == "buy" else self.service.sell
                )
                self._show_trade(operation(args.currency, amount))
            case "get-rate":
                result = self.service.get_rate(args.from_currency, args.to_currency)
                source, target = result["from_currency"], result["to_currency"]
                timestamp = result["updated_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
                self._print(
                    f"Курс {source}→{target}: {result['rate']:.8g} "
                    f"(обновлено: {timestamp})"
                )
                self._print(
                    f"Обратный курс {target}→{source}: {result['inverse_rate']:.8g}"
                )
            case "update-rates":
                result = self.service.update_rates(args.source)
                status = (
                    "завершено с ошибками" if result.failed_sources else "выполнено"
                )
                self._print(
                    f"Обновление {status}. Обновлено курсов: {result.changed_pairs}."
                )
                self._print(f"Последнее обновление: {result.last_refresh}")
                for reason in result.failed_sources.values():
                    self._print(reason)
                if result.failed_sources:
                    self._print("Подробности: parser.log в настроенном каталоге логов.")
            case "show-rates":
                self._show_rates(
                    self.service.show_rates(args.currency, args.top, args.base)
                )
        return True

    def _show_rates(self, result: dict) -> None:
        if result["empty"]:
            self._print(
                "Локальный кеш курсов пуст. Выполните 'update-rates', "
                "чтобы загрузить данные."
            )
            return
        self._print(f"Курсы из кеша (последнее обновление: {result['last_refresh']}):")
        if result["rows"]:
            table = PrettyTable(
                ["Пара", "Курс", "Источник", "Обновлено (UTC)", "Актуальность"]
            )
            for row in result["rows"]:
                table.add_row(
                    [
                        row["pair"],
                        f"{row['rate']:.10g}",
                        row["source"],
                        row["updated_at"].strftime("%Y-%m-%d %H:%M:%S"),
                        "УСТАРЕЛ" if row["stale"] else "Свежий",
                    ]
                )
            self._print(str(table))
        for code in result["missing"]:
            self._print(f"Курс для '{code}' не найден в кеше.")
        if not result["rows"] and not result["missing"]:
            self._print("Нет курсов, соответствующих фильтрам.")
        if any(row["stale"] for row in result["rows"]):
            self._print("Есть устаревшие данные. Выполните update-rates.")

    def _show_portfolio(self, result: dict) -> None:
        base = result["base_currency"]
        self._print(f"Портфель пользователя '{result['username']}' (база: {base}):")
        if not result["wallets"]:
            self._print("Портфель пуст. Купите валюту командой buy.")
        else:
            table = PrettyTable(["Валюта", "Баланс", f"Стоимость, {base}"])
            table.align = "r"
            table.align["Валюта"] = "l"
            for wallet in result["wallets"]:
                table.add_row(
                    [
                        wallet["currency_code"],
                        f"{wallet['balance']:,.8f}",
                        f"{wallet['value']:,.2f}",
                    ]
                )
            self._print(str(table))
        self._print(f"ИТОГО: {result['total']:,.2f} {base}")

    def _show_trade(self, result: dict) -> None:
        buying = result["operation"] == "buy"
        action = "Покупка выполнена" if buying else "Продажа выполнена"
        estimate = "Оценочная стоимость покупки" if buying else "Оценочная выручка"
        code = result["currency_code"]
        self._print(
            f"{action}: {result['amount']:.8f} {code} "
            f"по курсу {result['rate']:.8g} USD/{code}"
        )
        self._print("Изменения в портфеле:")
        self._print(
            f"- {code}: было {result['before']:.8f} → стало {result['after']:.8f}"
        )
        self._print(f"{estimate}: {result['estimated_value']:,.2f} USD")


def main() -> None:
    """Создать сервисы и запустить интерфейс из любой точки входа."""
    try:
        settings = SettingsLoader()
        setup_logging(settings)
        setup_parser_logging(settings)
        service = WalletService(settings=settings)
    except (ValueError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    WalletApplication(service).run()


if __name__ == "__main__":
    main()
