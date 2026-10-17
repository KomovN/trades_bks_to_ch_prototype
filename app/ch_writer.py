"""Асинхронный batch writer для bronze-строк ClickHouse.

Синхронный клиент выполняется в worker thread. Элемент очереди подтверждается
только после успешного возврата ``Client.insert``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import re
import time
from typing import Any, Protocol, TypeVar

import clickhouse_connect
from clickhouse_connect.driver.exceptions import (
    DataError,
    DatabaseError,
    IntegrityError,
    InterfaceError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)

from app.parser import TradeRawRow
from app.settings import Settings


INSERT_COLUMNS = (
    "received_at",
    "source",
    "ticker",
    "classCode",
    "event_time",
    "raw_json",
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PERMANENT_SERVER_CODES = frozenset(
    {
        16,  # NO_SUCH_COLUMN_IN_TABLE
        47,  # UNKNOWN_IDENTIFIER
        53,  # TYPE_MISMATCH
        60,  # UNKNOWN_TABLE
        62,  # SYNTAX_ERROR
        81,  # UNKNOWN_DATABASE
        117,  # INCORRECT_DATA
        164,  # READONLY
        497,  # ACCESS_DENIED
        516,  # AUTHENTICATION_FAILED
    }
)
_PERMANENT_SERVER_NAMES = frozenset(
    {
        "ACCESS_DENIED",
        "AUTHENTICATION_FAILED",
        "INCORRECT_DATA",
        "NO_SUCH_COLUMN_IN_TABLE",
        "READONLY",
        "SYNTAX_ERROR",
        "TYPE_MISMATCH",
        "UNKNOWN_DATABASE",
        "UNKNOWN_IDENTIFIER",
        "UNKNOWN_TABLE",
    }
)


class ClickHouseClient(Protocol):
    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        *,
        column_names: Sequence[str],
    ) -> Any: ...

    def close(self) -> Any: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, delay: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


@dataclass(frozen=True, slots=True)
class WriterOptions:
    table: str
    batch_size: int
    flush_interval_sec: float
    retry_initial_sec: float = 1.0
    retry_max_sec: float = 10.0

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.table):
            raise ValueError("table must be a simple SQL identifier")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if self.flush_interval_sec <= 0:
            raise ValueError("flush_interval_sec must be > 0")
        if self.retry_initial_sec <= 0:
            raise ValueError("retry_initial_sec must be > 0")
        if self.retry_max_sec < self.retry_initial_sec:
            raise ValueError("retry_max_sec must be >= retry_initial_sec")


@dataclass(frozen=True, slots=True)
class WriterResult:
    inserted_rows: int
    insert_attempts: int
    retries: int


@dataclass(frozen=True, slots=True)
class WriterSnapshot:
    """Безопасный моментальный снимок для периодического summary."""

    inserted_rows: int
    insert_attempts: int
    retries: int
    batch_size: int


class WriterError(RuntimeError):
    """Безопасная для логирования ошибка writer без тела ответа ClickHouse."""

    def __init__(self, message: str, *, unsaved_rows: int):
        super().__init__(message)
        self.unsaved_rows = unsaved_rows


class PermanentWriteError(WriterError):
    """Схема, доступ, конфигурация либо неожиданная ошибка клиента."""


class DrainDeadlineExceeded(WriterError):
    """Общий shutdown deadline истёк до подтверждения всех строк."""


class TemporaryClickHouseError(RuntimeError):
    """Явный маркер временного сбоя для адаптеров и тестовых клиентов."""


class PermanentClickHouseError(RuntimeError):
    """Явный маркер постоянного сбоя для адаптеров и тестовых клиентов."""


def row_values(row: TradeRawRow) -> tuple[Any, ...]:
    """Преобразовать строку в согласованный явный порядок INSERT-колонок."""

    return (
        row.received_at,
        row.source,
        row.ticker,
        row.class_code,
        row.event_time,
        row.raw_json,
    )


def insert_rows(
    client: ClickHouseClient,
    *,
    table: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    """Выполнить один синхронный INSERT; вызывающий код выносит его из event loop."""

    if not rows:
        raise ValueError("empty INSERT is forbidden")
    client.insert(table, rows, column_names=INSERT_COLUMNS)


def create_clickhouse_client(settings: Settings) -> ClickHouseClient:
    """Создать sync-клиент с конечными timeout и без скрытых query retry."""

    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password or "",
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
        connect_timeout=settings.clickhouse_connect_timeout_sec,
        send_receive_timeout=settings.clickhouse_io_timeout_sec,
        query_retries=0,
        show_clickhouse_errors="scrub",
    )


def classify_write_error(error: BaseException) -> str:
    """Вернуть ``temporary`` или ``permanent`` без анализа текста ошибки."""

    if isinstance(error, TemporaryClickHouseError):
        return "temporary"
    if isinstance(error, PermanentClickHouseError):
        return "permanent"
    if isinstance(error, (TimeoutError, ConnectionError, OperationalError)):
        return "temporary"
    if isinstance(
        error,
        (ProgrammingError, DataError, IntegrityError, InterfaceError, NotSupportedError),
    ):
        return "permanent"
    if isinstance(error, DatabaseError):
        code = getattr(error, "code", None)
        name = getattr(error, "name", None)
        if code in _PERMANENT_SERVER_CODES or name in _PERMANENT_SERVER_NAMES:
            return "permanent"
        # Неизвестный ответ сервера нельзя превращать в бесконечный retry.
        return "permanent"
    if isinstance(error, OSError):
        return "temporary"
    return "permanent"


_T = TypeVar("_T")


async def _run_sync_bounded(call: Callable[[], _T]) -> _T:
    """Не бросать запущенный thread при отмене coroutine."""

    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Отмена to_thread не останавливает sync I/O. Дожидаемся driver timeout,
        # чтобы после завершения writer не осталось бесконтрольного INSERT.
        try:
            await asyncio.shield(task)
        finally:
            raise


async def _cancel_tasks(tasks: Sequence[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_deadline(deadline: asyncio.Future[float]) -> float:
    """Ждать публикацию deadline, не отменяя shared Future вместе с waiter."""

    return await asyncio.shield(deadline)


class ClickHouseBatchWriter:
    """Один consumer, один текущий batch и не более одного INSERT одновременно."""

    def __init__(
        self,
        queue: asyncio.Queue[TradeRawRow],
        client_factory: Callable[[], ClickHouseClient],
        options: WriterOptions,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._queue = queue
        self._client_factory = client_factory
        self._options = options
        self._clock = SystemClock() if clock is None else clock
        self._client: ClickHouseClient | None = None
        self._inserted_rows = 0
        self._insert_attempts = 0
        self._retries = 0
        self._current_batch_size = 0

    def snapshot(self) -> WriterSnapshot:
        """Вернуть счётчики без доступа к payload текущего batch."""

        return WriterSnapshot(
            inserted_rows=self._inserted_rows,
            insert_attempts=self._insert_attempts,
            retries=self._retries,
            batch_size=self._current_batch_size,
        )

    async def run(
        self,
        *,
        producer_done: asyncio.Event,
        stop_requested: asyncio.Event,
        drain_deadline: asyncio.Future[float],
    ) -> WriterResult:
        """Писать до ``producer_done`` и пустой очереди.

        ``stop_requested`` лишь будит ожидание: он не доказывает, что producer уже
        перестал выполнять последний ``queue.put``. Supervisor публикует абсолютный
        monotonic deadline ровно один раз через ``drain_deadline``.
        """

        batch: list[TradeRawRow] = []
        stop_seen = stop_requested.is_set()
        first_row_at: float | None = None
        try:
            while True:
                self._raise_if_deadline_expired(drain_deadline, len(batch))

                while len(batch) < self._options.batch_size:
                    try:
                        row = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        row = None
                    if row is not None:
                        if not batch:
                            first_row_at = self._clock.monotonic()
                        batch.append(row)
                        self._current_batch_size = len(batch)
                        continue

                    if producer_done.is_set():
                        break
                    if batch and first_row_at is not None:
                        flush_at = first_row_at + self._options.flush_interval_sec
                        if self._clock.monotonic() >= flush_at:
                            break
                    else:
                        flush_at = None

                    event, value = await self._wait_for_activity(
                        producer_done=producer_done,
                        stop_requested=stop_requested,
                        stop_seen=stop_seen,
                        drain_deadline=drain_deadline,
                        flush_at=flush_at,
                    )
                    if event == "row":
                        if not batch:
                            first_row_at = self._clock.monotonic()
                        batch.append(value)
                        self._current_batch_size = len(batch)
                    elif event == "stop":
                        stop_seen = True
                    elif event == "deadline":
                        self._raise_deadline(len(batch))
                    elif event == "flush":
                        break

                if batch:
                    frozen_rows = tuple(row_values(row) for row in batch)
                    await self._insert_with_retry(
                        frozen_rows, drain_deadline=drain_deadline
                    )
                    for _ in batch:
                        self._queue.task_done()
                    self._inserted_rows += len(batch)
                    batch.clear()
                    self._current_batch_size = 0
                    first_row_at = None

                if producer_done.is_set() and self._queue.empty() and not batch:
                    return WriterResult(
                        inserted_rows=self._inserted_rows,
                        insert_attempts=self._insert_attempts,
                        retries=self._retries,
                    )
        finally:
            await self._discard_client()

    async def _insert_with_retry(
        self,
        frozen_rows: tuple[tuple[Any, ...], ...],
        *,
        drain_deadline: asyncio.Future[float],
    ) -> None:
        delay = self._options.retry_initial_sec
        while True:
            self._raise_if_deadline_expired(drain_deadline, len(frozen_rows))
            self._insert_attempts += 1
            try:
                if self._client is None:
                    self._client = await _run_sync_bounded(self._client_factory)
                client = self._client
                await _run_sync_bounded(
                    lambda: insert_rows(
                        client,
                        table=self._options.table,
                        rows=frozen_rows,
                    )
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._discard_client()
                if classify_write_error(error) == "permanent":
                    raise PermanentWriteError(
                        "permanent ClickHouse write failure",
                        unsaved_rows=len(frozen_rows) + self._queue.qsize(),
                    ) from None
                self._retries += 1
                await self._sleep_before_retry(
                    delay,
                    drain_deadline=drain_deadline,
                    batch_size=len(frozen_rows),
                )
                delay = min(delay * 2, self._options.retry_max_sec)

    async def _wait_for_activity(
        self,
        *,
        producer_done: asyncio.Event,
        stop_requested: asyncio.Event,
        stop_seen: bool,
        drain_deadline: asyncio.Future[float],
        flush_at: float | None,
    ) -> tuple[str, Any]:
        tagged: list[tuple[str, asyncio.Task[Any]]] = [
            ("row", asyncio.create_task(self._queue.get())),
            ("producer_done", asyncio.create_task(producer_done.wait())),
        ]
        if not stop_seen:
            tagged.append(("stop", asyncio.create_task(stop_requested.wait())))
        if not drain_deadline.done():
            tagged.append(
                ("deadline_set", asyncio.create_task(_await_deadline(drain_deadline)))
            )

        wake_at = flush_at
        deadline = self._deadline_value(drain_deadline)
        if deadline is not None and (wake_at is None or deadline < wake_at):
            wake_at = deadline
        if wake_at is not None:
            delay = max(0.0, wake_at - self._clock.monotonic())
            tagged.append(("timer", asyncio.create_task(self._clock.sleep(delay))))

        tasks = [task for _, task in tagged]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            winner_name = next(name for name, task in tagged if task in done)
            winner = next(task for _, task in tagged if task in done)
            value = winner.result()
        finally:
            await _cancel_tasks(tasks)

        if winner_name == "timer":
            deadline = self._deadline_value(drain_deadline)
            if deadline is not None and self._clock.monotonic() >= deadline:
                return "deadline", None
            return "flush", None
        if winner_name == "deadline_set":
            return await self._wait_for_activity(
                producer_done=producer_done,
                stop_requested=stop_requested,
                stop_seen=stop_seen,
                drain_deadline=drain_deadline,
                flush_at=flush_at,
            )
        return winner_name, value

    async def _sleep_before_retry(
        self,
        delay: float,
        *,
        drain_deadline: asyncio.Future[float],
        batch_size: int,
    ) -> None:
        while True:
            deadline = self._deadline_value(drain_deadline)
            if deadline is not None:
                remaining = deadline - self._clock.monotonic()
                if remaining <= 0:
                    self._raise_deadline(batch_size)
                if delay >= remaining:
                    await self._clock.sleep(remaining)
                    self._raise_deadline(batch_size)
                await self._clock.sleep(delay)
                return

            sleep_task = asyncio.create_task(self._clock.sleep(delay))
            if drain_deadline.done():
                await sleep_task
                return
            deadline_task = asyncio.create_task(_await_deadline(drain_deadline))
            wait_tasks = (sleep_task, deadline_task)
            try:
                done, _ = await asyncio.wait(
                    wait_tasks, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                await _cancel_tasks(wait_tasks)
                raise
            if sleep_task in done:
                await _cancel_tasks((deadline_task,))
                sleep_task.result()
                return
            sleep_task.cancel()
            await asyncio.gather(sleep_task, return_exceptions=True)

    def _deadline_value(
        self, drain_deadline: asyncio.Future[float]
    ) -> float | None:
        if not drain_deadline.done():
            return None
        if drain_deadline.cancelled():
            raise RuntimeError("drain_deadline must not be cancelled")
        return drain_deadline.result()

    def _raise_if_deadline_expired(
        self, drain_deadline: asyncio.Future[float], batch_size: int
    ) -> None:
        deadline = self._deadline_value(drain_deadline)
        if deadline is not None and self._clock.monotonic() >= deadline:
            self._raise_deadline(batch_size)

    def _raise_deadline(self, batch_size: int) -> None:
        raise DrainDeadlineExceeded(
            "ClickHouse drain deadline exceeded",
            unsaved_rows=batch_size + self._queue.qsize(),
        )

    async def _discard_client(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await _run_sync_bounded(client.close)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Ошибка close не меняет уже известный результат INSERT и не должна
            # скрывать исходную ошибку. Следующая попытка создаст новый клиент.
            pass
