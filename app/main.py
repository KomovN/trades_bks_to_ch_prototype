"""Composition root и ограниченный lifecycle ingest-сервиса."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import signal
import time
from typing import Protocol

from app.ch_writer import (
    ClickHouseBatchWriter,
    WriterOptions,
    WriterResult,
    WriterSnapshot,
    create_clickhouse_client,
)
from app.logging_setup import configure_logging
from app.parser import TradeRawRow
from app.settings import Settings, SettingsError, load_settings
from app.subscribe import SubscriptionError, load_subscription
from app.token_provider import AccessTokenProvider
from app.ws_reader import PermanentAuthError, ProducerCompletion, ReaderStats, WebSocketReader


logger = logging.getLogger(__name__)


class ReaderWorker(Protocol):
    stats: ReaderStats

    async def run(self, stop: asyncio.Event) -> None: ...


class WriterWorker(Protocol):
    def snapshot(self) -> WriterSnapshot: ...

    async def run(
        self,
        *,
        producer_done: asyncio.Event,
        stop_requested: asyncio.Event,
        drain_deadline: asyncio.Future[float],
    ) -> WriterResult: ...


@dataclass(frozen=True, slots=True)
class ServiceResult:
    exit_code: int
    reason: str
    received_frames: int
    accepted_trades: int
    inserted_rows: int
    unsaved_rows: int
    retries: int
    reconnects: int


class ShutdownController:
    """Публикует stop и единственный абсолютный monotonic deadline."""

    def __init__(
        self,
        stop: asyncio.Event,
        drain_deadline: asyncio.Future[float],
        timeout_sec: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stop = stop
        self.drain_deadline = drain_deadline
        self._timeout_sec = timeout_sec
        self._monotonic = monotonic
        self.reason: str | None = None

    def request(self, reason: str) -> float:
        if self.reason is None:
            self.reason = reason
        self.stop.set()
        if not self.drain_deadline.done():
            self.drain_deadline.set_result(self._monotonic() + self._timeout_sec)
        return self.drain_deadline.result()


def _safe_worker_failure(worker: str, error: BaseException) -> str:
    return f"{worker}_failure:{type(error).__name__}"


def _log_summary(
    reader: ReaderWorker,
    writer: WriterWorker,
    queue: asyncio.Queue[TradeRawRow],
    completion: ProducerCompletion,
    *,
    final: bool,
) -> None:
    read = reader.stats
    written = writer.snapshot()
    accepted_unsaved = max(0, read.accepted_trades - written.inserted_rows)
    logger.info(
        "event=ingest_summary final=%s received_frames=%d accepted_trades=%d "
        "inserted_rows=%d unsaved_rows=%d source_errors=%d skipped_control=%d "
        "skipped_other=%d skipped_malformed=%d skipped_malformed_trade=%d "
        "reconnects=%d planned_reconnects=%d write_retries=%d queue_size=%d "
        "batch_size=%d",
        str(final).lower(),
        read.received_frames,
        read.accepted_trades,
        written.inserted_rows,
        completion.unsaved_rows + accepted_unsaved,
        read.source_errors,
        read.skipped_control,
        read.skipped_other,
        read.skipped_malformed,
        read.skipped_malformed_trade,
        read.reconnects,
        read.planned_reconnects,
        written.retries,
        queue.qsize(),
        written.batch_size,
    )


async def _periodic_summaries(
    reader: ReaderWorker,
    writer: WriterWorker,
    queue: asyncio.Queue[TradeRawRow],
    completion: ProducerCompletion,
    interval_sec: float,
    finished: asyncio.Event,
) -> None:
    while True:
        try:
            await asyncio.wait_for(finished.wait(), timeout=interval_sec)
            return
        except TimeoutError:
            _log_summary(reader, writer, queue, completion, final=False)


async def supervise(
    *,
    reader: ReaderWorker,
    writer: WriterWorker,
    queue: asyncio.Queue[TradeRawRow],
    completion: ProducerCompletion,
    shutdown_timeout_sec: float,
    summary_interval_sec: float,
    auth_failed_exit_delay_sec: float,
    signal_requested: asyncio.Event | None = None,
) -> ServiceResult:
    """Следить за workers и завершить pipeline с bounded drain."""

    loop = asyncio.get_running_loop()
    signal_event = signal_requested or asyncio.Event()
    stop = asyncio.Event()
    drain_deadline: asyncio.Future[float] = loop.create_future()
    controller = ShutdownController(stop, drain_deadline, shutdown_timeout_sec)
    summaries_finished = asyncio.Event()

    reader_task = asyncio.create_task(reader.run(stop), name="bks-reader")
    writer_task = asyncio.create_task(
        writer.run(
            producer_done=completion.done,
            stop_requested=stop,
            drain_deadline=drain_deadline,
        ),
        name="clickhouse-writer",
    )
    signal_task = asyncio.create_task(signal_event.wait(), name="signal-waiter")
    summary_task = asyncio.create_task(
        _periodic_summaries(
            reader,
            writer,
            queue,
            completion,
            summary_interval_sec,
            summaries_finished,
        ),
        name="periodic-summary",
    )

    fatal = False
    permanent_auth = False
    writer_result: WriterResult | None = None
    reason = "signal"
    try:
        done, _ = await asyncio.wait(
            {reader_task, writer_task, signal_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        reader_error: BaseException | None = None
        writer_error: BaseException | None = None
        if reader_task in done:
            try:
                reader_task.result()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                reader_error = error
        if writer_task in done:
            try:
                writer_result = writer_task.result()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                writer_error = error

        if reader_error is not None:
            permanent_auth = isinstance(reader_error, PermanentAuthError)
            reason = _safe_worker_failure("reader", reader_error)
            fatal = True
        elif writer_error is not None:
            reason = _safe_worker_failure("writer", writer_error)
            fatal = True
        elif signal_task in done:
            reason = "signal"
        elif reader_task in done:
            reason = "reader_stopped"
            fatal = True
        elif writer_task in done:
            reason = "writer_stopped_before_reader"
            fatal = True

        deadline = controller.request(reason)
        workers = {reader_task, writer_task}
        unfinished = {task for task in workers if not task.done()}
        if unfinished:
            remaining = max(0.0, deadline - time.monotonic())
            _, unfinished = await asyncio.wait(unfinished, timeout=remaining)
        if unfinished:
            # Reader cleanup и bounded sync I/O writer-а идут параллельно. Поэтому
            # после 30-секундного drain добавляется максимум I/O budget, а не сумма
            # последовательных cleanup timeout обоих workers.
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)
            reason = "drain_deadline_exceeded"
            fatal = True

        if writer_task.done() and writer_result is None and not writer_task.cancelled():
            try:
                writer_result = writer_task.result()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                if not fatal:
                    reason = _safe_worker_failure("writer", error)
                fatal = True

        written = writer.snapshot()
        accepted_unsaved = max(0, reader.stats.accepted_trades - written.inserted_rows)
        unsaved = completion.unsaved_rows + accepted_unsaved
        if unsaved:
            fatal = True
            if reason == "signal":
                reason = "unsaved_rows"

        if permanent_auth and auth_failed_exit_delay_sec > 0 and not signal_event.is_set():
            try:
                await asyncio.wait_for(
                    signal_event.wait(), timeout=auth_failed_exit_delay_sec
                )
            except TimeoutError:
                pass

        return ServiceResult(
            exit_code=1 if fatal else 0,
            reason=reason,
            received_frames=reader.stats.received_frames,
            accepted_trades=reader.stats.accepted_trades,
            inserted_rows=written.inserted_rows,
            unsaved_rows=unsaved,
            retries=written.retries,
            reconnects=reader.stats.reconnects,
        )
    finally:
        controller.request(reason)
        summaries_finished.set()
        signal_task.cancel()
        if not reader_task.done():
            reader_task.cancel()
        if not writer_task.done():
            writer_task.cancel()
        await asyncio.gather(
            reader_task, writer_task, signal_task, summary_task,
            return_exceptions=True,
        )
        _log_summary(reader, writer, queue, completion, final=True)


async def run_service(settings: Settings, signal_requested: asyncio.Event) -> ServiceResult:
    subscription = load_subscription(settings.subscribe_config)
    queue: asyncio.Queue[TradeRawRow] = asyncio.Queue(
        maxsize=settings.queue_maxsize
    )
    completion = ProducerCompletion()
    token_provider = AccessTokenProvider(
        refresh_token=settings.refresh_token,
        static_access_token=settings.access_token,
        max_age_sec=settings.access_token_max_age_sec,
    )
    reader = WebSocketReader(
        url=settings.ws_url,
        source=settings.source,
        subscription=subscription,
        token_provider=token_provider,
        queue=queue,
        completion=completion,
        reconnect_min_delay_sec=settings.reconnect_min_delay_sec,
        reconnect_max_delay_sec=settings.reconnect_max_delay_sec,
    )
    writer = ClickHouseBatchWriter(
        queue,
        lambda: create_clickhouse_client(settings),
        WriterOptions(
            table=settings.clickhouse_table,
            batch_size=settings.batch_size,
            flush_interval_sec=settings.flush_interval_sec,
        ),
    )
    return await supervise(
        reader=reader,
        writer=writer,
        queue=queue,
        completion=completion,
        shutdown_timeout_sec=settings.shutdown_timeout_sec,
        summary_interval_sec=settings.log_summary_interval_sec,
        auth_failed_exit_delay_sec=settings.auth_failed_exit_delay_sec,
        signal_requested=signal_requested,
    )


async def _run_with_signals(settings: Settings) -> ServiceResult:
    signal_requested = asyncio.Event()
    installed = install_signal_handlers(signal_requested)
    try:
        return await run_service(settings, signal_requested)
    finally:
        remove_signal_handlers(installed)


def install_signal_handlers(signal_requested: asyncio.Event) -> list[signal.Signals]:
    """Перенаправить SIGTERM/SIGINT в event loop без async-работы в handler-е."""

    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, signal_requested.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)
    return installed


def remove_signal_handlers(installed: list[signal.Signals]) -> None:
    loop = asyncio.get_running_loop()
    for signum in installed:
        loop.remove_signal_handler(signum)


def main() -> int:
    try:
        settings = load_settings()
    except SettingsError:
        configure_logging("INFO")
        logger.error("event=startup_failure reason=invalid_settings")
        return 1

    configure_logging(
        settings.log_level,
        secrets=(
            settings.access_token,
            settings.refresh_token,
            settings.clickhouse_password,
        ),
    )
    try:
        result = asyncio.run(_run_with_signals(settings))
    except SubscriptionError:
        logger.error("event=startup_failure reason=invalid_subscription")
        return 1
    except Exception as error:
        logger.error("event=service_failure reason=%s", type(error).__name__)
        return 1
    logger.info(
        "event=service_exit code=%d reason=%s accepted_trades=%d "
        "inserted_rows=%d unsaved_rows=%d",
        result.exit_code,
        result.reason,
        result.accepted_trades,
        result.inserted_rows,
        result.unsaved_rows,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
