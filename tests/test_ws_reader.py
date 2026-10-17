from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.parser import TradeRawRow
from app.subscribe import load_subscription
from app.token_provider import TemporaryAuthError
from app.ws_reader import (
    HandshakeError,
    PermanentAuthError,
    PermanentSubscriptionError,
    ProducerCompletion,
    ReaderConfigurationError,
    WS_MAX_QUEUE_FRAMES,
    WS_MAX_SIZE_BYTES,
    WebSocketClosed,
    WebSocketReader,
    websocket_connector,
)


ROOT = Path(__file__).parents[1]
SUBSCRIPTION = load_subscription(ROOT / "subscribe_msg.yaml")
RECEIVED_AT = datetime(2026, 10, 17, 12, 30, 45, 123456, tzinfo=timezone.utc)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def trade_frame(*, ticker: str = "SBER") -> str:
    return json.dumps(
        {
            "responseType": "LastTrades",
            "ticker": ticker,
            "classCode": "TQBR",
            "dateTime": "2026-10-17T12:30:00.100Z",
            "side": "Buy",
            "price": 316.5300,
            "quantity": 10,
        },
        separators=(",", ":"),
    )


def source_error(code: int) -> str:
    return json.dumps({"error": {"code": code}}, separators=(",", ":"))


class FakeTokenProvider:
    def __init__(
        self,
        *,
        static: bool = False,
        ttls: list[float | None] | None = None,
        token_errors: list[Exception] | None = None,
    ) -> None:
        self.is_static = static
        self._ttls = list(ttls or [])
        self._token_errors = list(token_errors or [])
        self.get_calls = 0
        self.invalidations = 0

    async def get_token(self) -> str:
        self.get_calls += 1
        if self._token_errors:
            error = self._token_errors.pop(0)
            raise error
        return f"token-{self.get_calls}"

    def ttl(self) -> float | None:
        return self._ttls.pop(0) if self._ttls else None

    def invalidate(self) -> bool:
        if self.is_static:
            return False
        self.invalidations += 1
        return True


class FakeWebSocket:
    def __init__(
        self,
        events: list[str | bytes | Exception],
        *,
        stop: asyncio.Event | None = None,
        stop_on_recv: bool = False,
    ) -> None:
        self.events = list(events)
        self.sent: list[str] = []
        self.stop = stop
        self.stop_on_recv = stop_on_recv

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self.events:
            await asyncio.Future()
        event = self.events.pop(0)
        if self.stop_on_recv and self.stop is not None:
            self.stop.set()
        if isinstance(event, Exception):
            raise event
        return event


class SequenceConnector:
    def __init__(
        self,
        sessions: list[FakeWebSocket | Exception],
        *,
        stop: asyncio.Event | None = None,
        stop_on_entry: int | None = None,
    ) -> None:
        self.sessions = list(sessions)
        self.calls: list[tuple[str, str]] = []
        self.stop = stop
        self.stop_on_entry = stop_on_entry

    def __call__(self, url: str, token: str):
        @asynccontextmanager
        async def context():
            self.calls.append((url, token))
            if self.stop_on_entry == len(self.calls) and self.stop is not None:
                self.stop.set()
            if not self.sessions:
                raise AssertionError("unexpected connection")
            session = self.sessions.pop(0)
            if isinstance(session, Exception):
                raise session
            yield session

        return context()


def make_reader(
    *,
    connector,
    stop: asyncio.Event,
    queue: asyncio.Queue[TradeRawRow] | None = None,
    completion: ProducerCompletion | None = None,
    provider: FakeTokenProvider | None = None,
    jitter=None,
    monotonic=None,
    wall_clock=None,
    minimum: float = 0.001,
    maximum: float = 0.01,
) -> WebSocketReader:
    return WebSocketReader(
        url="wss://example.test/ws",
        source="bks",
        subscription=SUBSCRIPTION,
        token_provider=provider or FakeTokenProvider(),  # type: ignore[arg-type]
        queue=queue or asyncio.Queue(maxsize=10),
        completion=completion or ProducerCompletion(),
        reconnect_min_delay_sec=minimum,
        reconnect_max_delay_sec=maximum,
        connector=connector,
        jitter=jitter or (lambda base, maximum: base),
        monotonic=monotonic or (lambda: 0.0),
        wall_clock=wall_clock or (lambda: RECEIVED_AT),
    )


@async_test
async def test_production_connector_sets_auth_and_bounded_buffers(monkeypatch) -> None:
    captured = {}

    class Connection:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    def fake_connect(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Connection()

    monkeypatch.setattr("app.ws_reader.connect", fake_connect)
    async with websocket_connector("wss://example.test/ws", "secret"):
        pass

    assert captured["max_size"] == WS_MAX_SIZE_BYTES == 1024 * 1024
    assert captured["max_queue"] == WS_MAX_QUEUE_FRAMES == 16
    assert captured["additional_headers"] == {"Authorization": "Bearer secret"}


@async_test
async def test_received_at_is_captured_immediately_after_recv_and_raw_is_preserved() -> None:
    stop = asyncio.Event()
    frame = trade_frame()
    calls: list[str] = []

    class OrderedWebSocket(FakeWebSocket):
        async def recv(self):
            result = await super().recv()
            calls.append("recv")
            return result

    def wall_clock():
        calls.append("clock")
        stop.set()
        return RECEIVED_AT

    websocket = OrderedWebSocket([frame])
    connector = SequenceConnector([websocket])
    queue: asyncio.Queue[TradeRawRow] = asyncio.Queue(maxsize=10)
    completion = ProducerCompletion()
    reader = make_reader(
        connector=connector,
        stop=stop,
        queue=queue,
        completion=completion,
        wall_clock=wall_clock,
    )

    await reader.run(stop)

    row = queue.get_nowait()
    assert calls == ["recv", "clock"]
    assert row.received_at == RECEIVED_AT.replace(microsecond=123000)
    assert row.raw_json == frame
    assert completion.done.is_set()
    assert completion.unsaved_rows == 0


@async_test
async def test_disconnect_resubscribes_full_snapshot_and_backoff_grows() -> None:
    stop = asyncio.Event()
    sessions = [
        FakeWebSocket([WebSocketClosed(1006)]),
        FakeWebSocket([WebSocketClosed(1006)]),
        FakeWebSocket([], stop=stop),
    ]
    connector = SequenceConnector(sessions, stop=stop, stop_on_entry=3)
    bases: list[float] = []

    def jitter(base: float, maximum: float) -> float:
        bases.append(base)
        return 0.0001

    reader = make_reader(connector=connector, stop=stop, jitter=jitter)
    await reader.run(stop)

    assert bases == [0.001, 0.002]
    assert len(connector.calls) == 3
    for websocket in sessions:
        assert len(websocket.sent) == 1
        payload = json.loads(websocket.sent[0])
        assert len(payload["instruments"]) == 101
        assert payload["subscribeType"] == 0
        assert payload["dataType"] == 2


@async_test
async def test_backoff_resets_only_after_stable_session() -> None:
    stop = asyncio.Event()
    first = FakeWebSocket([WebSocketClosed(1006)])
    second = FakeWebSocket([WebSocketClosed(1006)])
    third = FakeWebSocket([])
    connector = SequenceConnector(
        [first, second, third], stop=stop, stop_on_entry=3
    )
    times = iter([0.0, 1.0, 10.0, 41.0, 50.0])
    bases: list[float] = []

    reader = make_reader(
        connector=connector,
        stop=stop,
        minimum=0.001,
        maximum=0.1,
        monotonic=lambda: next(times),
        jitter=lambda base, maximum: bases.append(base) or 0.0001,
    )
    await reader.run(stop)

    assert bases == [0.001, 0.001]


@async_test
async def test_no_data_keeps_same_idle_connection_without_reconnect() -> None:
    stop = asyncio.Event()

    class NoDataThenIdle(FakeWebSocket):
        async def recv(self):
            if self.events:
                value = await super().recv()
                asyncio.get_running_loop().call_soon(stop.set)
                return value
            return await super().recv()

    websocket = NoDataThenIdle([source_error(1)])
    connector = SequenceConnector([websocket])
    reader = make_reader(connector=connector, stop=stop)

    await reader.run(stop)

    assert len(connector.calls) == 1
    assert reader.stats.no_data == 1
    assert reader.stats.source_errors == 1
    assert reader.stats.reconnects == 0
    assert reader.stats.accepted_trades == 0


@async_test
@pytest.mark.parametrize("code", [2, 3])
async def test_wrong_subscription_is_fatal(code: int) -> None:
    stop = asyncio.Event()
    completion = ProducerCompletion()
    connector = SequenceConnector([FakeWebSocket([source_error(code)])])
    reader = make_reader(
        connector=connector, stop=stop, completion=completion
    )

    with pytest.raises(PermanentSubscriptionError, match=str(code)):
        await reader.run(stop)

    assert completion.done.is_set()
    assert reader.stats.reconnects == 0


@async_test
@pytest.mark.parametrize("status", [401, 403])
async def test_static_handshake_auth_failure_is_immediately_fatal(status: int) -> None:
    stop = asyncio.Event()
    provider = FakeTokenProvider(static=True)
    connector = SequenceConnector([HandshakeError(status)])
    reader = make_reader(connector=connector, stop=stop, provider=provider)

    with pytest.raises(PermanentAuthError):
        await reader.run(stop)

    assert provider.get_calls == 1
    assert provider.invalidations == 0


@async_test
async def test_refresh_auth_is_forced_once_then_second_rejection_is_fatal() -> None:
    stop = asyncio.Event()
    provider = FakeTokenProvider()
    connector = SequenceConnector(
        [HandshakeError(401), FakeWebSocket([source_error(403)])]
    )
    reader = make_reader(connector=connector, stop=stop, provider=provider)

    with pytest.raises(PermanentAuthError):
        await reader.run(stop)

    assert provider.get_calls == 2
    assert provider.invalidations == 1
    assert len(connector.calls) == 2


@async_test
async def test_auth_close_is_distinct_from_regular_close() -> None:
    stop = asyncio.Event()
    static_provider = FakeTokenProvider(static=True)
    auth_reader = make_reader(
        connector=SequenceConnector([FakeWebSocket([WebSocketClosed(4401)])]),
        stop=stop,
        provider=static_provider,
    )

    with pytest.raises(PermanentAuthError):
        await auth_reader.run(stop)

    stop = asyncio.Event()
    normal_connector = SequenceConnector(
        [FakeWebSocket([WebSocketClosed(1006)]), FakeWebSocket([])],
        stop=stop,
        stop_on_entry=2,
    )
    normal_reader = make_reader(connector=normal_connector, stop=stop)
    await normal_reader.run(stop)
    assert normal_reader.stats.reconnects == 1


@async_test
async def test_unknown_structured_source_error_has_bounded_reconnect() -> None:
    stop = asyncio.Event()
    connector = SequenceConnector(
        [FakeWebSocket([source_error(0)]), FakeWebSocket([])],
        stop=stop,
        stop_on_entry=2,
    )
    reader = make_reader(connector=connector, stop=stop)

    await reader.run(stop)

    assert reader.stats.source_errors == 1
    assert reader.stats.reconnects == 1
    assert len(connector.calls) == 2


@async_test
async def test_planned_reconnect_before_token_deadline_resubscribes() -> None:
    stop = asyncio.Event()
    provider = FakeTokenProvider(ttls=[0.0, 0.0])
    first = FakeWebSocket([])
    second = FakeWebSocket([])
    connector = SequenceConnector(
        [first, second], stop=stop, stop_on_entry=2
    )
    reader = make_reader(connector=connector, stop=stop, provider=provider)

    await reader.run(stop)

    assert reader.stats.planned_reconnects == 1
    assert reader.stats.reconnects == 0
    assert len(first.sent) == len(second.sent) == 1
    assert provider.get_calls == 2


@async_test
async def test_long_idle_does_not_create_rows_or_reconnect() -> None:
    stop = asyncio.Event()
    websocket = FakeWebSocket([])
    connector = SequenceConnector([websocket])
    queue: asyncio.Queue[TradeRawRow] = asyncio.Queue(maxsize=1)
    reader = make_reader(connector=connector, stop=stop, queue=queue)

    task = asyncio.create_task(reader.run(stop))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    stop.set()
    await task

    assert len(connector.calls) == 1
    assert queue.empty()
    assert reader.stats.received_frames == 0
    assert reader.stats.reconnects == 0


@async_test
async def test_stop_interrupts_blocked_put_and_accounts_unsaved_row() -> None:
    stop = asyncio.Event()
    queue: asyncio.Queue[TradeRawRow] = asyncio.Queue(maxsize=1)
    sentinel = TradeRawRow(
        received_at=RECEIVED_AT,
        source="bks",
        ticker="OLD",
        class_code="TQBR",
        event_time=RECEIVED_AT,
        raw_json="{}",
    )
    queue.put_nowait(sentinel)
    completion = ProducerCompletion()
    websocket = FakeWebSocket(
        [trade_frame()], stop=stop, stop_on_recv=True
    )
    reader = make_reader(
        connector=SequenceConnector([websocket]),
        stop=stop,
        queue=queue,
        completion=completion,
    )

    await reader.run(stop)

    assert queue.qsize() == 1
    assert queue.get_nowait() is sentinel
    assert completion.unsaved_rows == 1
    assert completion.done.is_set()
    assert reader.stats.accepted_trades == 0


@async_test
async def test_completed_put_owns_row_when_stop_completes_in_same_tick() -> None:
    stop = asyncio.Event()
    queue: asyncio.Queue[TradeRawRow] = asyncio.Queue(maxsize=1)
    completion = ProducerCompletion()
    websocket = FakeWebSocket(
        [trade_frame()], stop=stop, stop_on_recv=True
    )
    reader = make_reader(
        connector=SequenceConnector([websocket]),
        stop=stop,
        queue=queue,
        completion=completion,
    )

    await reader.run(stop)

    assert queue.qsize() == 1
    assert completion.unsaved_rows == 0
    assert reader.stats.accepted_trades == 1


@async_test
async def test_auth_like_text_inside_trade_is_not_treated_as_auth() -> None:
    stop = asyncio.Event()
    queue: asyncio.Queue[TradeRawRow] = asyncio.Queue(maxsize=1)
    websocket = FakeWebSocket(
        [trade_frame(ticker="401-secret-looking")], stop=stop, stop_on_recv=True
    )
    reader = make_reader(
        connector=SequenceConnector([websocket]), stop=stop, queue=queue
    )

    await reader.run(stop)

    assert queue.get_nowait().ticker == "401-secret-looking"
    assert reader.stats.accepted_trades == 1


@async_test
async def test_temporary_token_error_uses_bounded_retry_after() -> None:
    stop = asyncio.Event()
    provider = FakeTokenProvider(
        token_errors=[TemporaryAuthError("temporary", retry_after_sec=50.0)]
    )
    connector = SequenceConnector(
        [FakeWebSocket([])], stop=stop, stop_on_entry=1
    )
    bases: list[float] = []
    reader = make_reader(
        connector=connector,
        stop=stop,
        provider=provider,
        minimum=0.001,
        maximum=0.01,
        jitter=lambda base, maximum: bases.append(base) or 0.0001,
    )

    await reader.run(stop)

    assert bases == [0.01]
    assert provider.get_calls == 2


def test_reader_rejects_unbounded_queue() -> None:
    stop = asyncio.Event()
    with pytest.raises(ReaderConfigurationError, match="bounded"):
        make_reader(
            connector=SequenceConnector([]),
            stop=stop,
            queue=asyncio.Queue(),
        )
