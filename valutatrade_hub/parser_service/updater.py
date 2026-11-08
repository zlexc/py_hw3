"""Независимый опрос источников, журнал измерений и публикация снимка."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from valutatrade_hub.core import utils
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.storage import StorageError
from valutatrade_hub.logging_config import PARSER_LOGGER_NAME
from valutatrade_hub.parser_service.api_clients import (
    BaseApiClient,
    CoinGeckoClient,
    ExchangeRateApiClient,
    iso_utc,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import ParserStorage


@dataclass(frozen=True)
class UpdateResult:
    changed_pairs: int
    history_added: int
    failed_sources: dict[str, str]
    last_refresh: str | None


class RatesUpdater:
    def __init__(
        self,
        clients: list[BaseApiClient],
        storage: ParserStorage,
        *,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] = perf_counter,
    ) -> None:
        self.clients = clients
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timer = timer

    def _log(self, event: str, *, error=False, **context) -> None:
        logging.getLogger(PARSER_LOGGER_NAME).log(
            logging.ERROR if error else logging.INFO,
            "",
            extra={"parser_data": {"event": event, **context}},
        )

    def run_update(
        self, source: str | None = None, *, sources: set[str] | None = None
    ) -> UpdateResult:
        selected = sources if sources is not None else ({source} if source else None)
        clients = [
            client
            for client in self.clients
            if selected is None or client.source_key in selected
        ]
        if not clients:
            raise ApiRequestError("Для выбранного источника не настроены валюты")
        if selected and selected - {client.source_key for client in clients}:
            raise ApiRequestError("Для нужного источника не настроены валюты")
        started = self.timer()
        self._log("update_start")
        # Повреждённые файлы обнаруживаются до сетевых запросов и любых записей.
        try:
            self.storage.load_cache()
            self.storage.load_history()
        except StorageError:
            self._log(
                "storage_validation_failed",
                error=True,
                result="ERROR",
                error_type="StorageError",
            )
            raise
        measurements, failures = [], {}
        for client in clients:
            self._log("fetch_start", source=client.source)
            try:
                quotes = client.fetch_rates()
                entries = []
                if not quotes:
                    raise ApiRequestError(f"{client.source}: пустой ответ")
                for pair, rate in quotes.items():
                    rate = utils.number(rate, positive=True)
                    source_code, target_code = pair.split("_")
                    metadata = client.last_fetch[pair].copy()
                    timestamp = iso_utc(utils.parse_time(metadata.pop("timestamp")))
                    entries.append(
                        {
                            "id": f"{pair}_{timestamp}",
                            "from_currency": source_code,
                            "to_currency": target_code,
                            "rate": rate,
                            "timestamp": timestamp,
                            "source": client.source,
                            "meta": metadata,
                        }
                    )
                    self.storage.validate_measurement(entries[-1])
            except (ApiRequestError, ValueError, TypeError, KeyError) as error:
                # ApiRequestError клиента уже санитизирован; сырые ошибки не выводим.
                reason = (
                    error.reason
                    if isinstance(error, ApiRequestError)
                    else (f"{client.source}: не удалось получить корректные курсы")
                )
                failures[client.source_key] = reason
                self._log(
                    "fetch_failed",
                    source=client.source,
                    error=True,
                    result="ERROR",
                    error_type="ApiRequestError",
                    error_message=reason,
                )
                continue
            measurements.extend(entries)
            self._log(
                "fetch_success",
                source=client.source,
                rates_count=len(entries),
                result="OK",
            )
        if not measurements:
            self._log(
                "update_finished",
                error=True,
                result="ERROR",
                rates_count=0,
                duration_ms=round((self.timer() - started) * 1000),
            )
            raise ApiRequestError(
                "Не удалось обновить курсы: " + "; ".join(failures.values())
            )
        try:
            changed, added, last_refresh = self.storage.commit(
                measurements, self.clock()
            )
        except StorageError:
            self._log(
                "persistence_failed",
                error=True,
                result="ERROR",
                error_type="StorageError",
            )
            raise
        self._log(
            "snapshot_saved", rates_count=changed, history_added=added, result="OK"
        )
        self._log(
            "update_finished",
            result="PARTIAL" if failures else "OK",
            rates_count=changed,
            duration_ms=round((self.timer() - started) * 1000),
        )
        return UpdateResult(changed, added, failures, last_refresh)

    def refresh(self, sources: set[str]) -> UpdateResult:
        return self.run_update(sources=sources)


def build_updater(config: ParserConfig, *, session=None, clock=None) -> RatesUpdater:
    clients = []
    if config.crypto_currencies:
        clients.append(CoinGeckoClient(config, session=session, clock=clock))
    if config.fiat_currencies:
        clients.append(ExchangeRateApiClient(config, session=session, clock=clock))
    return RatesUpdater(clients, ParserStorage(config.data_dir), clock=clock)
