import asyncio
from datetime import datetime, timezone
import threading
import time
from typing import Any

from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError, ProgrammingError
import pytest

from app.ch_writer import (
    INSERT_COLUMNS,
    ClickHouseBatchWriter,
    DrainDeadlineExceeded,
    PermanentWriteError,
    TemporaryClickHouseError,
    WriterOptions,
    classify_write_error,
    insert_rows,
    row_values,
)
from app.parser import TradeRawRow


def make_row(number: int) -> TradeRawRow:
    return TradeRawRow(
        received_at=datetime(2026, 10, 17, 10, 0, 0, number * 1000, tzinfo=timezone.utc),
        source="bks",
        ticker=f"T{number}",
        class_code="TQBR",
        event_time=datetime(2026, 10, 17, 9, 0, 0, number * 1000, tzinfo=timezone.utc),
        raw_json=f'{{"n":{number}, "price":316.5300}}',
    )


class FakeClient:
    def __init__(self, effects: list[BaseException | None] | None = None) -> None:
        self.effects = list(effects or [])
        self.calls: list[tuple[str, tuple[tuple[Any, ...], ...], tuple[str, ...]]] = []
        self.closed = 0

    def insert(
        self,
        table: str,
        data: object,
        *,
        column_names: object,
    ) -> None:
        rows = tuple(tuple(row) for row in data)  # type: ignore[union-attr]
        columns = tuple(column_names)  # type: ignore[arg-type]
        self.calls.append((table, rows, columns))
        if self.effects:
            effect = self.effects.pop(0)
            if effect is not None:
                raise effect

    def close(self) -> None:
        self.closed += 1


class AdvancingClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay
        await asyncio.sleep(0)


class ManualClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        target = self.value + delay
        self._sleepers.append((target, future))
        self._wake_due()
        await future

    def advance(self, delay: float) -> None:
        self.value += delay
        self._wake_due()

    def _wake_due(self) -> None:
        for target, future in self._sleepers:
            if target <= self.value and not future.done():
                future.set_result(None)


def make_writer(
    queue: asyncio.Queue[TradeRawRow],
    factory: object,
    *,
    batch_size: int = 2,
    flush_interval: float = 10.0,
    clock: object | None = None,
) -> ClickHouseBatchWriter:
    return ClickHouseBatchWriter(
        queue,
        factory,  # type: ignore[arg-type]
        WriterOptions(
            table="trades_raw_json",
            batch_size=batch_size,
            flush_interval_sec=flush_interval,
            retry_initial_sec=1.0,
            retry_max_sec=4.0,
        ),
        clock=clock,  # type: ignore[arg-type]
    )


def lifecycle() -> tuple[asyncio.Event, asyncio.Event, asyncio.Future[float]]:
    loop = asyncio.get_running_loop()
    return asyncio.Event(), asyncio.Event(), loop.create_future()


async def spin_until(predicate: object, *, turns: int = 100) -> None:
    for _ in range(turns):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def test_explicit_column_order_and_raw_values() -> None:
    row = make_row(1)
    client = FakeClient()

    insert_rows(client, table="trades_raw_json", rows=(row_values(row),))

    assert client.calls == [
        ("trades_raw_json", (row_values(row),), INSERT_COLUMNS)
    ]
    assert client.calls[0][1][0][-1] is row.raw_json
    with pytest.raises(ValueError, match="empty INSERT"):
        insert_rows(client, table="trades_raw_json", rows=())


def test_flushes_at_batch_size_and_acknowledges_after_insert() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        rows = [make_row(1), make_row(2)]
        for row in rows:
            await queue.put(row)
        producer_done, stop, deadline = lifecycle()
        producer_done.set()
        client = FakeClient()

        result = await make_writer(queue, lambda: client).run(
            producer_done=producer_done,
            stop_requested=stop,
            drain_deadline=deadline,
        )

        assert result.inserted_rows == 2
        assert result.insert_attempts == 1
        assert client.calls[0][1] == tuple(row_values(row) for row in rows)
        await asyncio.wait_for(queue.join(), timeout=0.1)

    asyncio.run(scenario())


def test_flush_interval_is_measured_from_first_row() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        await queue.put(make_row(1))
        producer_done, stop, deadline = lifecycle()
        clock = ManualClock()
        client = FakeClient()
        task = asyncio.create_task(
            make_writer(
                queue,
                lambda: client,
                batch_size=10,
                flush_interval=5.0,
                clock=clock,
            ).run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )
        )

        await spin_until(lambda: bool(clock._sleepers))
        clock.advance(4.9)
        await asyncio.sleep(0)
        assert client.calls == []
        clock.advance(0.1)
        await spin_until(lambda: len(client.calls) == 1)
        producer_done.set()
        result = await asyncio.wait_for(task, timeout=1)

        assert result.inserted_rows == 1
        assert len(client.calls) == 1

    asyncio.run(scenario())


def test_producer_completion_flushes_residual_batch_without_empty_insert() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        await queue.put(make_row(1))
        producer_done, stop, deadline = lifecycle()
        client = FakeClient()
        producer_done.set()

        result = await make_writer(queue, lambda: client, batch_size=10).run(
            producer_done=producer_done,
            stop_requested=stop,
            drain_deadline=deadline,
        )

        assert result.inserted_rows == 1
        assert len(client.calls) == 1

        empty_queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        empty_done, empty_stop, empty_deadline = lifecycle()
        empty_done.set()
        empty_client = FakeClient()
        empty_result = await make_writer(empty_queue, lambda: empty_client).run(
            producer_done=empty_done,
            stop_requested=empty_stop,
            drain_deadline=empty_deadline,
        )
        assert empty_result.insert_attempts == 0
        assert empty_client.calls == []

    asyncio.run(scenario())


def test_uncertain_commit_retries_identical_batch_and_allows_duplicates() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        rows = [make_row(1), make_row(2)]
        for row in rows:
            await queue.put(row)
        producer_done, stop, deadline = lifecycle()
        producer_done.set()
        committed_then_lost = FakeClient([OperationalError("response lost")])
        retry_client = FakeClient()
        clients = iter((committed_then_lost, retry_client))
        clock = AdvancingClock()

        result = await make_writer(queue, lambda: next(clients), clock=clock).run(
            producer_done=producer_done,
            stop_requested=stop,
            drain_deadline=deadline,
        )

        # Первая запись могла попасть в CH до потери ответа. Повтор намеренно
        # побайтно/позначно идентичен и потому может создать допустимые дубли.
        assert committed_then_lost.calls[0] == retry_client.calls[0]
        assert result.insert_attempts == 2
        assert result.retries == 1
        assert clock.sleeps == [1.0]
        assert committed_then_lost.closed == 1
        assert retry_client.closed == 1
        await asyncio.wait_for(queue.join(), timeout=0.1)

    asyncio.run(scenario())


def test_queue_join_waits_for_insert_ack() -> None:
    class BlockingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def insert(self, table: str, data: object, *, column_names: object) -> None:
            self.entered.set()
            assert self.release.wait(timeout=2)
            super().insert(table, data, column_names=column_names)

    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        await queue.put(make_row(1))
        producer_done, stop, deadline = lifecycle()
        producer_done.set()
        client = BlockingClient()
        writer = make_writer(queue, lambda: client, batch_size=1)
        writer_task = asyncio.create_task(
            writer.run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )
        )
        join_task = asyncio.create_task(queue.join())

        await asyncio.to_thread(client.entered.wait, 1)
        await asyncio.sleep(0)
        assert not join_task.done()
        assert writer.snapshot().batch_size == 1
        assert writer.snapshot().inserted_rows == 0
        client.release.set()
        await writer_task
        await asyncio.wait_for(join_task, timeout=0.2)
        assert writer.snapshot().batch_size == 0
        assert writer.snapshot().inserted_rows == 1

    asyncio.run(scenario())


def test_permanent_failure_is_not_retried_or_acknowledged() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        await queue.put(make_row(1))
        producer_done, stop, deadline = lifecycle()
        producer_done.set()
        client = FakeClient([ProgrammingError("unknown table")])

        with pytest.raises(PermanentWriteError) as raised:
            await make_writer(queue, lambda: client, batch_size=1).run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )

        assert raised.value.unsaved_rows == 1
        assert str(raised.value) == "permanent ClickHouse write failure"
        assert len(client.calls) == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.join(), timeout=0.01)

    asyncio.run(scenario())


def test_stop_does_not_replace_producer_done() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        producer_done, stop, deadline = lifecycle()
        stop.set()
        client = FakeClient()
        task = asyncio.create_task(
            make_writer(queue, lambda: client, batch_size=1).run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )
        )

        await asyncio.sleep(0)
        assert not task.done()
        await queue.put(make_row(1))
        producer_done.set()
        result = await asyncio.wait_for(task, timeout=1)

        assert result.inserted_rows == 1
        assert len(client.calls) == 1

    asyncio.run(scenario())


def test_common_deadline_stops_retry_and_leaves_item_unacknowledged() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        await queue.put(make_row(1))
        producer_done, stop, deadline = lifecycle()
        producer_done.set()
        stop.set()
        clock = AdvancingClock()
        deadline.set_result(clock.monotonic() + 0.5)
        client = FakeClient([TemporaryClickHouseError("down")])

        with pytest.raises(DrainDeadlineExceeded) as raised:
            await make_writer(queue, lambda: client, batch_size=1, clock=clock).run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )

        assert raised.value.unsaved_rows == 1
        assert len(client.calls) == 1
        assert clock.sleeps == [0.5]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.join(), timeout=0.01)

    asyncio.run(scenario())


def test_sync_insert_does_not_block_event_loop() -> None:
    class SlowClient(FakeClient):
        def insert(self, table: str, data: object, *, column_names: object) -> None:
            time.sleep(0.05)
            super().insert(table, data, column_names=column_names)

    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        await queue.put(make_row(1))
        producer_done, stop, deadline = lifecycle()
        producer_done.set()
        heartbeats = 0

        async def heartbeat() -> None:
            nonlocal heartbeats
            end = asyncio.get_running_loop().time() + 0.04
            while asyncio.get_running_loop().time() < end:
                heartbeats += 1
                await asyncio.sleep(0)

        writer_task = asyncio.create_task(
            make_writer(queue, SlowClient, batch_size=1).run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )
        )
        await asyncio.gather(writer_task, heartbeat())
        assert heartbeats > 10

    asyncio.run(scenario())


def test_cancellation_waits_for_inflight_sync_insert_to_finish() -> None:
    class BlockingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def insert(self, table: str, data: object, *, column_names: object) -> None:
            self.entered.set()
            assert self.release.wait(timeout=2)
            super().insert(table, data, column_names=column_names)
            self.finished.set()

    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        await queue.put(make_row(1))
        producer_done, stop, deadline = lifecycle()
        producer_done.set()
        client = BlockingClient()
        task = asyncio.create_task(
            make_writer(queue, lambda: client, batch_size=1).run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )
        )

        assert await asyncio.to_thread(client.entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        client.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.finished.is_set()
        assert client.closed == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.join(), timeout=0.01)

    asyncio.run(scenario())


def test_idle_cancellation_removes_pending_queue_get() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[TradeRawRow] = asyncio.Queue()
        producer_done, stop, deadline = lifecycle()
        client = FakeClient()
        task = asyncio.create_task(
            make_writer(queue, lambda: client).run(
                producer_done=producer_done,
                stop_requested=stop,
                drain_deadline=deadline,
            )
        )

        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        row = make_row(1)
        await queue.put(row)
        await asyncio.sleep(0)
        assert queue.get_nowait() is row
        assert client.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OperationalError("network"), "temporary"),
        (TimeoutError(), "temporary"),
        (ProgrammingError("schema"), "permanent"),
        (DatabaseError("hidden", code=60, name="UNKNOWN_TABLE"), "permanent"),
        (DatabaseError("unknown server response", code=999), "permanent"),
        (RuntimeError("programming bug"), "permanent"),
    ],
)
def test_error_classification_is_explicit(
    error: BaseException, expected: str
) -> None:
    assert classify_write_error(error) == expected


@pytest.mark.parametrize(
    "options",
    [
        WriterOptions,
    ],
)
def test_writer_options_reject_unsafe_or_unbounded_values(options: object) -> None:
    constructor = options  # type: ignore[assignment]
    with pytest.raises(ValueError):
        constructor(table="db.table", batch_size=1, flush_interval_sec=1)
    with pytest.raises(ValueError):
        constructor(table="trades_raw_json", batch_size=0, flush_interval_sec=1)
    with pytest.raises(ValueError):
        constructor(table="trades_raw_json", batch_size=1, flush_interval_sec=0)
    with pytest.raises(ValueError):
        constructor(
            table="trades_raw_json",
            batch_size=1,
            flush_interval_sec=1,
            retry_initial_sec=2,
            retry_max_sec=1,
        )
