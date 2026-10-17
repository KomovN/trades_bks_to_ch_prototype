"""WebSocket producer: subscription, classification and bounded reconnects."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import random
import time
from typing import Protocol

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from app.parser import ParseResult, TradeRawRow, parse_message
from app.subscribe import EXPECTED_INSTRUMENT_COUNT, Subscription
from app.token_provider import (
    AccessTokenProvider,
    AuthConfigurationError,
    AuthError,
    TemporaryAuthError,
)


logger = logging.getLogger(__name__)

WS_MAX_SIZE_BYTES = 1024 * 1024
WS_MAX_QUEUE_FRAMES = 16
STABLE_SESSION_SEC = 30.0
_AUTH_STATUS_CODES = frozenset({401, 403})
_AUTH_CLOSE_CODES = frozenset({1008, 4001, 4003, 4401, 4403})
_NO_DATA_CODE = 1
_FATAL_SUBSCRIPTION_CODES = frozenset({2, 3})


class ReaderError(RuntimeError):
    """Fatal, safely printable reader failure."""


class PermanentAuthError(ReaderError):
    """Credentials were rejected and cannot be retried by this reader."""


class PermanentSubscriptionError(ReaderError):
    """BKS rejected the subscription as NotFound or IncorrectJson."""


class ReaderConfigurationError(ReaderError):
    """Token provider or reader wiring is invalid."""


class HandshakeError(OSError):
    """Sanitized injectable handshake failure used by alternate transports."""

    def __init__(self, status_code: int | None = None) -> None:
        super().__init__("WebSocket handshake failed")
        self.status_code = status_code


class WebSocketClosed(OSError):
    """Sanitized injectable close used by alternate transports."""

    def __init__(self, code: int | None = None) -> None:
        super().__init__("WebSocket connection closed")
        self.code = code


class WebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


WsConnector = Callable[
    [str, str], AbstractAsyncContextManager[WebSocket]
]
WallClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
Jitter = Callable[[float, float], float]


@dataclass(slots=True)
class ProducerCompletion:
    """Handshake between the sole producer and the draining writer/supervisor.

    ``done`` is set only after the reader cannot enqueue another row. A trade
    observed during a stop race is either in the queue or included in
    ``unsaved_rows`` before ``done`` is set.
    """

    done: asyncio.Event = field(default_factory=asyncio.Event)
    unsaved_rows: int = 0

    def account_unsaved(self) -> None:
        self.unsaved_rows += 1

    def finish(self) -> None:
        self.done.set()


@dataclass(slots=True)
class ReaderStats:
    received_frames: int = 0
    accepted_trades: int = 0
    source_errors: int = 0
    no_data: int = 0
    reconnects: int = 0
    planned_reconnects: int = 0
    skipped_control: int = 0
    skipped_other: int = 0
    skipped_malformed: int = 0
    skipped_malformed_trade: int = 0


def _default_jitter(base_delay: float, maximum: float) -> float:
    return min(maximum, base_delay + random.uniform(0.0, base_delay * 0.2))


@asynccontextmanager
async def websocket_connector(url: str, token: str) -> AsyncIterator[WebSocket]:
    """Open one bounded-buffer WS connection without exposing credentials."""

    async with connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=WS_MAX_SIZE_BYTES,
        max_queue=WS_MAX_QUEUE_FRAMES,
    ) as websocket:
        yield websocket


class _AuthRejected(Exception):
    pass


class _RetryableSourceError(Exception):
    pass


class _SessionEnded(Exception):
    pass


class _SessionOutcome:
    STOP = "stop"
    ROTATE = "rotate"


class WebSocketReader:
    """Read one BKS stream and enqueue parsed trades with bounded backpressure."""

    def __init__(
        self,
        *,
        url: str,
        source: str,
        subscription: Subscription,
        token_provider: AccessTokenProvider,
        queue: asyncio.Queue[TradeRawRow],
        completion: ProducerCompletion,
        reconnect_min_delay_sec: float,
        reconnect_max_delay_sec: float,
        connector: WsConnector = websocket_connector,
        wall_clock: WallClock = lambda: datetime.now(timezone.utc),
        monotonic: MonotonicClock = time.monotonic,
        jitter: Jitter = _default_jitter,
        stable_session_sec: float = STABLE_SESSION_SEC,
    ) -> None:
        if len(subscription.instruments) != EXPECTED_INSTRUMENT_COUNT:
            raise ReaderConfigurationError("subscription must contain 101 instruments")
        if queue.maxsize <= 0:
            raise ReaderConfigurationError("reader queue must be bounded")
        if not math.isfinite(reconnect_min_delay_sec) or reconnect_min_delay_sec <= 0:
            raise ReaderConfigurationError("minimum reconnect delay must be positive")
        if (
            not math.isfinite(reconnect_max_delay_sec)
            or reconnect_max_delay_sec < reconnect_min_delay_sec
        ):
            raise ReaderConfigurationError("maximum reconnect delay is invalid")
        if not math.isfinite(stable_session_sec) or stable_session_sec < 0:
            raise ReaderConfigurationError("stable session threshold is invalid")

        self._url = url
        self._source = source
        self._subscription_json = subscription.to_json()
        self._token_provider = token_provider
        self._queue = queue
        self._completion = completion
        self._min_delay = float(reconnect_min_delay_sec)
        self._max_delay = float(reconnect_max_delay_sec)
        self._connector = connector
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._jitter = jitter
        self._stable_session_sec = float(stable_session_sec)
        self.stats = ReaderStats()
        self._running = False

    async def run(self, stop: asyncio.Event) -> None:
        """Run until stop or a fatal error; always signal producer completion."""

        if self._running or self._completion.done.is_set():
            raise ReaderConfigurationError("reader is single-use")
        self._running = True
        retry_index = 0
        auth_retry_used = False
        try:
            while not stop.is_set():
                try:
                    token = await self._token_provider.get_token()
                except TemporaryAuthError as exc:
                    delay = self._retry_delay(retry_index, exc.retry_after_sec)
                    retry_index += 1
                    self.stats.reconnects += 1
                    if await self._wait_or_stop(stop, delay):
                        return
                    continue
                except AuthError:
                    raise PermanentAuthError("token endpoint rejected credentials") from None
                except AuthConfigurationError:
                    raise ReaderConfigurationError("token provider configuration failed") from None

                session_started = self._monotonic()
                try:
                    async with self._connector(self._url, token) as websocket:
                        await websocket.send(self._subscription_json)
                        outcome = await self._receive_session(websocket, stop)
                except PermanentSubscriptionError:
                    raise
                except _AuthRejected:
                    stable = self._is_stable(session_started)
                    if stable:
                        retry_index = 0
                        auth_retry_used = False
                    if self._token_provider.is_static or auth_retry_used:
                        raise PermanentAuthError("WebSocket authentication was rejected") from None
                    self._token_provider.invalidate()
                    auth_retry_used = True
                    continue
                except (HandshakeError, WebSocketClosed) as exc:
                    if self._is_auth_transport_error(exc):
                        if self._token_provider.is_static or auth_retry_used:
                            raise PermanentAuthError(
                                "WebSocket authentication was rejected"
                            ) from None
                        self._token_provider.invalidate()
                        auth_retry_used = True
                        continue
                    outcome = None
                except InvalidStatus as exc:
                    if self._status_code(exc) in _AUTH_STATUS_CODES:
                        if self._token_provider.is_static or auth_retry_used:
                            raise PermanentAuthError(
                                "WebSocket authentication was rejected"
                            ) from None
                        self._token_provider.invalidate()
                        auth_retry_used = True
                        continue
                    outcome = None
                except ConnectionClosed as exc:
                    if self._close_code(exc) in _AUTH_CLOSE_CODES:
                        if self._token_provider.is_static or auth_retry_used:
                            raise PermanentAuthError(
                                "WebSocket authentication was rejected"
                            ) from None
                        self._token_provider.invalidate()
                        auth_retry_used = True
                        continue
                    outcome = None
                except (InvalidHandshake, OSError, TimeoutError, asyncio.TimeoutError):
                    outcome = None
                except _RetryableSourceError:
                    outcome = None
                except _SessionEnded:
                    outcome = None

                if outcome == _SessionOutcome.STOP:
                    return
                if self._is_stable(session_started):
                    retry_index = 0
                    auth_retry_used = False
                if outcome == _SessionOutcome.ROTATE:
                    self.stats.planned_reconnects += 1
                    continue

                delay = self._retry_delay(retry_index)
                retry_index += 1
                self.stats.reconnects += 1
                if await self._wait_or_stop(stop, delay):
                    return
        finally:
            self._completion.finish()

    async def _receive_session(
        self, websocket: WebSocket, stop: asyncio.Event
    ) -> str:
        while True:
            ttl = self._token_provider.ttl()
            recv_task = asyncio.create_task(websocket.recv())
            stop_task = asyncio.create_task(stop.wait())
            rotation_task: asyncio.Task[None] | None = None
            if ttl is not None:
                rotation_task = asyncio.create_task(asyncio.sleep(max(0.0, ttl)))
            tasks = {recv_task, stop_task}
            if rotation_task is not None:
                tasks.add(rotation_task)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            # A completed recv owns its frame even when stop completes in the same tick.
            if recv_task in done:
                try:
                    raw_msg = recv_task.result()
                    received_at = self._wall_clock()
                finally:
                    await self._cancel_tasks(pending)
                self.stats.received_frames += 1
                result = parse_message(
                    raw_msg, source=self._source, received_at=received_at
                )
                should_stop = await self._handle_result(result, stop)
                if should_stop:
                    return _SessionOutcome.STOP
                continue

            await self._cancel_tasks(pending)
            if stop_task in done and stop_task.result():
                return _SessionOutcome.STOP
            return _SessionOutcome.ROTATE

    async def _handle_result(
        self, result: ParseResult, stop: asyncio.Event
    ) -> bool:
        if result.kind == "trade":
            assert result.row is not None
            accepted = await self._put_or_stop(result.row, stop)
            if accepted:
                self.stats.accepted_trades += 1
                return stop.is_set()
            self._completion.account_unsaved()
            return True
        if result.kind == "source_error":
            self.stats.source_errors += 1
            if result.error_code == _NO_DATA_CODE:
                self.stats.no_data += 1
                return False
            if result.error_code in _FATAL_SUBSCRIPTION_CODES:
                raise PermanentSubscriptionError(
                    f"subscription rejected with code {result.error_code}"
                )
            if result.error_code in _AUTH_STATUS_CODES:
                raise _AuthRejected
            raise _RetryableSourceError
        if result.kind == "control":
            self.stats.skipped_control += 1
        elif result.kind == "other":
            self.stats.skipped_other += 1
        elif result.kind == "malformed":
            self.stats.skipped_malformed += 1
        elif result.kind == "malformed_trade":
            self.stats.skipped_malformed_trade += 1
        return False

    async def _put_or_stop(self, row: TradeRawRow, stop: asyncio.Event) -> bool:
        put_task = asyncio.create_task(self._queue.put(row))
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {put_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if put_task in done:
            await self._cancel_tasks(pending)
            put_task.result()
            return True
        await self._cancel_tasks(pending)
        return False

    async def _wait_or_stop(self, stop: asyncio.Event, delay: float) -> bool:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    def _retry_delay(self, retry_index: int, minimum: float | None = None) -> float:
        base = min(self._max_delay, self._min_delay * (2 ** min(retry_index, 30)))
        if minimum is not None:
            base = min(self._max_delay, max(base, minimum))
        delay = self._jitter(base, self._max_delay)
        if not math.isfinite(delay):
            return base
        return min(self._max_delay, max(self._min_delay, delay))

    def _is_stable(self, started: float) -> bool:
        return self._monotonic() - started >= self._stable_session_sec

    @staticmethod
    async def _cancel_tasks(tasks: set[asyncio.Task[object]]) -> None:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _status_code(exc: InvalidStatus) -> int | None:
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
        return value if isinstance(value, int) else None

    @staticmethod
    def _close_code(exc: ConnectionClosed) -> int | None:
        received = getattr(exc, "rcvd", None)
        value = getattr(received, "code", None)
        return value if isinstance(value, int) else None

    @staticmethod
    def _is_auth_transport_error(exc: HandshakeError | WebSocketClosed) -> bool:
        code = getattr(exc, "status_code", None)
        if code in _AUTH_STATUS_CODES:
            return True
        return getattr(exc, "code", None) in _AUTH_CLOSE_CODES
