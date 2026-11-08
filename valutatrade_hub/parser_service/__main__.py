"""Запуск отдельного Parser Service через python -m."""

from valutatrade_hub.parser_service.scheduler import main

if __name__ == "__main__":
    raise SystemExit(main())
