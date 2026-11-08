"""Однократный запуск и последовательный foreground-планировщик."""

import argparse
import sys
from threading import Event

from valutatrade_hub.core import utils
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import setup_parser_logging
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.updater import RatesUpdater, build_updater


def run_scheduler(
    updater: RatesUpdater,
    *,
    interval: float = 3600,
    watch=False,
    source=None,
    wait=None,
    report=print,
) -> int:
    interval = utils.number(interval, positive=True)
    if interval > 86400 * 365:
        raise ValueError("Интервал не должен превышать один год")
    waiter = wait if wait is not None else Event().wait
    while True:
        try:
            result = updater.run_update(source)
            partial = bool(result.failed_sources)
            report(
                f"Обновление {'завершено с ошибками' if partial else 'выполнено'}. "
                f"Обновлено курсов: {result.changed_pairs}. "
                f"Последнее обновление: {result.last_refresh}."
            )
            if partial:
                report("Подробности: parser.log в настроенном каталоге логов.")
            code = 2 if partial else 0
        except ApiRequestError as error:
            report(str(error))
            code = 1
        if not watch:
            return code
        if waiter(interval):
            return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Parser Service: обновление курсов")
    parser.add_argument("--source", choices=("coingecko", "exchangerate"))
    parser.add_argument("--watch", action="store_true", help="повторять до Ctrl+C")
    parser.add_argument("--interval", type=float, help="интервал watch-режима, секунды")
    args = parser.parse_args(argv)
    if args.interval is not None and not args.watch:
        parser.error("--interval используется только вместе с --watch")
    try:
        settings = SettingsLoader()
        config = ParserConfig.from_settings(settings)
        interval = (
            config.update_interval_seconds if args.interval is None else args.interval
        )
        interval = utils.number(interval, positive=True)
        if interval > 86400 * 365:
            raise ValueError("Интервал не должен превышать один год")
        setup_parser_logging(settings)
        return run_scheduler(
            build_updater(config),
            interval=interval,
            watch=args.watch,
            source=args.source,
        )
    except KeyboardInterrupt:
        print("\nParser Service остановлен.")
        return 0
    except (ValueError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
