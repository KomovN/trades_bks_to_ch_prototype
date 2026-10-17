from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

from app.ch_writer import WriterResult, WriterSnapshot
from app.main import ServiceResult, supervise
from app.parser import TradeRawRow
from app.ws_reader import PermanentAuthError, ProducerCompletion, ReaderStats


ROOT = Path(__file__).parents[1]


def make_row() -> TradeRawRow:
    instant = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
    return TradeRawRow(
        received_at=instant,
        source="bks",
        ticker="SBER",
        class_code="TQBR",
        event_time=instant,
        raw_json='{"ticker":"SBER","price":316.5300}',
    )


class FakeReader:
    def __init__(
        self,
        completion: ProducerCompletion,
        queue: asyncio.Queue[TradeRawRow],
        *,
        rows: int = 0,
        failure: BaseException | None = None,
    ) -> None:
        self.completion = completion
        self.queue = queue
        self.rows = rows
        self.failure = failure
        self.stats = ReaderStats()

    async def run(self, stop: asyncio.Event) -> None:
        try:
            for _ in range(self.rows):
                await self.queue.put(make_row())
                self.stats.received_frames += 1
                self.stats.accepted_trades += 1
            if self.failure is not None:
                raise self.failure
            await stop.wait()
        finally:
            self.completion.finish()


class FakeWriter:
    def __init__(
        self,
        queue: asyncio.Queue[TradeRawRow],
        *,
        failure: BaseException | None = None,
        fail_immediately: bool = False,
    ) -> None:
        self.queue = queue
        self.failure = failure
        self.fail_immediately = fail_immediately
        self.inserted = 0
        self.attempts = 0
        self.retries = 0
        self.batch = 0

    def snapshot(self) -> WriterSnapshot:
        return WriterSnapshot(
            inserted_rows=self.inserted,
            insert_attempts=self.attempts,
            retries=self.retries,
            batch_size=self.batch,
        )

    async def run(self, *, producer_done, stop_requested, drain_deadline):
        if self.fail_immediately:
            assert self.failure is not None
            raise self.failure
        await producer_done.wait()
        while not self.queue.empty():
            self.batch = 1
            self.queue.get_nowait()
            self.attempts += 1
            if self.failure is not None:
                raise self.failure
            self.inserted += 1
            self.batch = 0
            self.queue.task_done()
        return WriterResult(self.inserted, self.attempts, self.retries)


class DeadlineWriter(FakeWriter):
    async def run(self, *, producer_done, stop_requested, drain_deadline):
        await producer_done.wait()
        self.batch = self.queue.qsize()
        deadline = await asyncio.shield(drain_deadline)
        self.retries += 1
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        raise TimeoutError("fake bounded ClickHouse outage")


async def run_scenario(
    reader: FakeReader,
    writer: FakeWriter,
    queue: asyncio.Queue[TradeRawRow],
    completion: ProducerCompletion,
    signal_event: asyncio.Event,
    *,
    timeout: float = 0.1,
    auth_delay: float = 0.0,
    summary_interval: float = 60.0,
) -> ServiceResult:
    return await supervise(
        reader=reader,
        writer=writer,
        queue=queue,
        completion=completion,
        shutdown_timeout_sec=timeout,
        summary_interval_sec=summary_interval,
        auth_failed_exit_delay_sec=auth_delay,
        signal_requested=signal_event,
    )


def test_signal_stops_reader_then_drains_all_accepted_rows() -> None:
    async def scenario() -> None:
        queue = asyncio.Queue(maxsize=2)
        completion = ProducerCompletion()
        signal_event = asyncio.Event()
        reader = FakeReader(completion, queue, rows=2)
        writer = FakeWriter(queue)
        asyncio.get_running_loop().call_later(0.01, signal_event.set)

        result = await run_scenario(reader, writer, queue, completion, signal_event)

        assert result.exit_code == 0
        assert result.reason == "signal"
        assert result.accepted_trades == result.inserted_rows == 2
        assert result.unsaved_rows == 0

    asyncio.run(scenario())


def test_reader_crash_is_fatal_but_writer_drains_owned_row() -> None:
    async def scenario() -> None:
        queue = asyncio.Queue(maxsize=1)
        completion = ProducerCompletion()
        signal_event = asyncio.Event()
        reader = FakeReader(completion, queue, rows=1, failure=ValueError("raw secret"))
        writer = FakeWriter(queue)

        result = await run_scenario(reader, writer, queue, completion, signal_event)

        assert result.exit_code == 1
        assert result.reason == "reader_failure:ValueError"
        assert result.accepted_trades == result.inserted_rows == 1
        assert result.unsaved_rows == 0

    asyncio.run(scenario())


def test_writer_crash_stops_reader_and_counts_accepted_row_unsaved() -> None:
    async def scenario() -> None:
        queue = asyncio.Queue(maxsize=1)
        completion = ProducerCompletion()
        signal_event = asyncio.Event()
        reader = FakeReader(completion, queue, rows=1)
        writer = FakeWriter(queue, failure=RuntimeError("secret"), fail_immediately=True)

        result = await run_scenario(reader, writer, queue, completion, signal_event)

        assert result.exit_code == 1
        assert result.reason == "writer_failure:RuntimeError"
        assert result.accepted_trades == 1
        assert result.inserted_rows == 0
        assert result.unsaved_rows == 1
        assert completion.done.is_set()

    asyncio.run(scenario())


def test_shared_deadline_bounds_retry_and_preserves_unsaved_count() -> None:
    async def scenario() -> None:
        queue = asyncio.Queue(maxsize=1)
        completion = ProducerCompletion()
        signal_event = asyncio.Event()
        reader = FakeReader(completion, queue, rows=1)
        writer = DeadlineWriter(queue)
        asyncio.get_running_loop().call_later(0.01, signal_event.set)
        started = time.monotonic()

        result = await run_scenario(
            reader, writer, queue, completion, signal_event, timeout=0.05
        )

        assert time.monotonic() - started < 0.2
        assert result.exit_code == 1
        assert result.unsaved_rows == 1
        assert result.retries == 1

    asyncio.run(scenario())


def test_permanent_auth_delay_is_interrupted_by_later_signal() -> None:
    async def scenario() -> None:
        queue = asyncio.Queue(maxsize=1)
        completion = ProducerCompletion()
        signal_event = asyncio.Event()
        reader = FakeReader(
            completion, queue, failure=PermanentAuthError("sanitized")
        )
        writer = FakeWriter(queue)
        asyncio.get_running_loop().call_later(0.03, signal_event.set)
        started = time.monotonic()

        result = await run_scenario(
            reader,
            writer,
            queue,
            completion,
            signal_event,
            auth_delay=10.0,
        )

        assert time.monotonic() - started < 0.2
        assert result.exit_code == 1
        assert result.reason == "reader_failure:PermanentAuthError"

    asyncio.run(scenario())


def test_idle_periodic_summary_is_rate_limited_and_final_is_once(caplog) -> None:
    async def scenario() -> ServiceResult:
        queue = asyncio.Queue(maxsize=1)
        completion = ProducerCompletion()
        signal_event = asyncio.Event()
        reader = FakeReader(completion, queue)
        writer = FakeWriter(queue)
        asyncio.get_running_loop().call_later(0.075, signal_event.set)
        return await run_scenario(
            reader,
            writer,
            queue,
            completion,
            signal_event,
            summary_interval=0.03,
        )

    with caplog.at_level(logging.INFO, logger="app.main"):
        result = asyncio.run(scenario())

    summaries = [r.message for r in caplog.records if "event=ingest_summary" in r.message]
    assert result.exit_code == 0
    assert len([message for message in summaries if "final=false" in message]) == 2
    assert len([message for message in summaries if "final=true" in message]) == 1
    assert all("raw_json" not in message for message in summaries)


_PROCESS_PROGRAM = r'''
import asyncio
from datetime import datetime, timezone
import signal
import sys

import clickhouse_connect

from app.ch_writer import ClickHouseBatchWriter, WriterOptions, WriterResult, WriterSnapshot
from app.main import install_signal_handlers, remove_signal_handlers, supervise
from app.parser import TradeRawRow
from app.ws_reader import ProducerCompletion, ReaderStats

MODE = sys.argv[1]

class Reader:
    def __init__(self, completion, queue):
        self.completion = completion
        self.queue = queue
        self.stats = ReaderStats()

    async def run(self, stop):
        try:
            if MODE == "ch-down":
                now = datetime.now(timezone.utc)
                await self.queue.put(TradeRawRow(now, "bks", "SBER", "TQBR", now, "{}"))
                self.stats.received_frames = 1
                self.stats.accepted_trades = 1
            print("READY", flush=True)
            await stop.wait()
        finally:
            self.completion.finish()

class EmptyWriter:
    def snapshot(self):
        return WriterSnapshot(0, 0, 0, 0)

    async def run(self, *, producer_done, stop_requested, drain_deadline):
        await producer_done.wait()
        return WriterResult(0, 0, 0)

async def run():
    signal_event = asyncio.Event()
    installed = install_signal_handlers(signal_event)
    queue = asyncio.Queue(maxsize=1)
    completion = ProducerCompletion()
    reader = Reader(completion, queue)
    if MODE == "ch-down":
        def factory():
            return clickhouse_connect.get_client(
                host="127.0.0.1", port=9, connect_timeout=5,
                send_receive_timeout=10, query_retries=0,
                show_clickhouse_errors="scrub",
            )
        writer = ClickHouseBatchWriter(
            queue, factory,
            WriterOptions(
                table="trades_raw_json", batch_size=1,
                flush_interval_sec=1, retry_initial_sec=1, retry_max_sec=10,
            ),
        )
    else:
        writer = EmptyWriter()
    try:
        result = await supervise(
            reader=reader, writer=writer, queue=queue, completion=completion,
            shutdown_timeout_sec=30, summary_interval_sec=60,
            auth_failed_exit_delay_sec=600, signal_requested=signal_event,
        )
    finally:
        remove_signal_handlers(installed)
    raise SystemExit(result.exit_code)

asyncio.run(run())
'''


def _run_signalled_process(mode: str, signum: signal.Signals) -> tuple[int, float]:
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_PROCESS_PROGRAM), mode],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "READY"
    started = time.monotonic()
    process.send_signal(signum)
    try:
        return_code = process.wait(timeout=45.0)
    except BaseException:
        process.kill()
        process.wait()
        raise
    return return_code, time.monotonic() - started


def test_actual_sigint_process_exits_cleanly() -> None:
    return_code, elapsed = _run_signalled_process("clean", signal.SIGINT)
    assert return_code == 0
    assert elapsed < 5.0


def test_actual_sigterm_with_unavailable_clickhouse_obeys_default_45s_bound() -> None:
    return_code, elapsed = _run_signalled_process("ch-down", signal.SIGTERM)
    assert return_code == 1
    assert 25.0 <= elapsed <= 45.0
