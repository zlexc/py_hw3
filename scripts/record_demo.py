"""Запись настоящей CLI-сессии в asciicast v2 с временными данными и живыми API."""

import codecs
import errno
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

from valutatrade_hub.parser_service.config import ParserConfig

DEMO_COMMANDS = (
    "register --username demo_user --password demo1234",
    "login --username demo_user --password demo1234",
    "update-rates",
    "show-rates --top 2",
    "buy --currency BTC --amount 0.05",
    "sell --currency BTC --amount 0.01",
    "show-portfolio",
    "show-portfolio --base BTC",
    "get-rate --from USD --to BTC",
    "sell --currency BTC --amount 1",
    "buy --currency ABC --amount 1",
    "show-rates --currency RUB",
    "exit",
)
TERMINAL_WIDTH = 140
TERMINAL_HEIGHT = 40


def record_demo(output: Path) -> None:
    """Записать вывод PTY в asciicast; ключи и данные пользователя не публиковать."""
    config = ParserConfig.from_settings({})
    if not config.exchangerate_api_key:
        raise ValueError("Для записи демо задайте EXCHANGERATE_API_KEY в .env")
    environment = {
        **os.environ,
        "EXCHANGERATE_API_KEY": config.exchangerate_api_key,
        "COINGECKO_API_KEY": config.coingecko_api_key,
        "TERM": "xterm-256color",
        "PYTHONUTF8": "1",
    }
    header = {
        "version": 2,
        "width": TERMINAL_WIDTH,
        "height": TERMINAL_HEIGHT,
        "timestamp": int(time.time()),
        "title": "ValutaTrade Hub — полный цикл, живые API и обработка ошибок",
        "env": {"TERM": "xterm-256color"},
    }
    events = []
    started = time.monotonic()
    decoder = codecs.getincrementaldecoder("utf-8")()
    master, slave = pty.openpty()
    fcntl.ioctl(
        slave,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", TERMINAL_HEIGHT, TERMINAL_WIDTH, 0, 0),
    )
    with tempfile.TemporaryDirectory(prefix="valutatrade-demo-") as directory:
        process = subprocess.Popen(
            [sys.executable, "-m", "finalproject_simonov_m26_555"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=directory,
            env=environment,
        )
        os.close(slave)

        def _read_until(marker: str) -> None:
            collected = ""
            deadline = time.monotonic() + 45
            while marker not in collected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("CLI не ответил в течение 45 секунд")
                if not select.select([master], [], [], min(remaining, 1))[0]:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                if not chunk:
                    raise RuntimeError("CLI завершился до окончания сценария")
                text = decoder.decode(chunk)
                if text:
                    events.append([round(time.monotonic() - started, 6), "o", text])
                    collected += text

        try:
            _read_until("> ")
            for command in DEMO_COMMANDS:
                time.sleep(1.2)
                os.write(master, (command + "\n").encode())
                _read_until("До свидания!" if command == "exit" else "> ")
            if process.wait(timeout=5) != 0:
                raise RuntimeError("CLI завершился с ошибкой")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
    transcript = "".join(event[2] for event in events)
    required = (
        "зарегистрирован (id=1)",
        "Вы вошли как 'demo_user'",
        "Обновлено курсов: 6",
        "Покупка выполнена",
        "Продажа выполнена",
        "(база: BTC)",
        "Курс USD→BTC:",
        "Недостаточно средств",
        "Неизвестная валюта 'ABC'",
        "RUB_USD",
    )
    if any(text not in transcript for text in required) or "Traceback" in transcript:
        raise RuntimeError(
            "Не все сценарии демо завершились успешно; запись не сохранена"
        )
    for secret in (config.exchangerate_api_key, config.coingecko_api_key):
        if secret and secret in transcript:
            raise RuntimeError("В выводе обнаружен ключ API; запись не сохранена")
    output.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in [header, *events])
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Сохранить проверенную демонстрацию в demo.cast в корне проекта."""
    output = Path(__file__).resolve().parents[1] / "demo.cast"
    try:
        record_demo(output)
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
    print(f"Демонстрация сохранена: {output.name}")


if __name__ == "__main__":
    main()
