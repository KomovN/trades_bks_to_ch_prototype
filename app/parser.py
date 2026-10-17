"""Чистая классификация входящих WebSocket frames в bronze-строки."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Literal, TypeAlias


ParseKind: TypeAlias = Literal[
    "trade",
    "control",
    "other",
    "malformed",
    "malformed_trade",
    "source_error",
]

_TRADE_RESPONSE_TYPES = frozenset({"LastTrades", "LastTradesSuccess"})
_TRADE_DISCRIMINATORS = frozenset({"side", "price", "quantity"})
_TIMEZONE_SUFFIX = re.compile(r"(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")


@dataclass(frozen=True, slots=True)
class TradeRawRow:
    """Одна immutable строка для INSERT в согласованном порядке колонок."""

    received_at: datetime
    source: str
    ticker: str
    class_code: str
    event_time: datetime
    raw_json: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Результат классификации без включения исходного сообщения в причины ошибок."""

    kind: ParseKind
    row: TradeRawRow | None = None
    error_code: int | None = None
    reason: str | None = None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _utc_milliseconds(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        offset = None
    if offset is None:
        raise ValueError(f"{field} must include a UTC offset")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.replace(microsecond=(utc_value.microsecond // 1000) * 1000)


def _parse_event_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if _TIMEZONE_SUFFIX.search(value) is None:
        return None
    iso_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(iso_value)
        return _utc_milliseconds(parsed, field="dateTime")
    except (OverflowError, ValueError):
        return None


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


def _source_error_code(error: object) -> int | None:
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def parse_message(
    raw_msg: str | bytes, *, source: str, received_at: datetime
) -> ParseResult:
    """Классифицировать frame и создать row только для валидной сделки.

    ``received_at`` фиксируется reader-ом до вызова этой функции. Невалидные
    значения аргументов reader-а считаются нарушением интерфейса и вызывают
    ``ValueError``; ошибки самого frame возвращаются как ``ParseResult``.
    """

    normalized_received_at = _utc_milliseconds(received_at, field="received_at")
    if not isinstance(source, str) or not source or source != source.strip():
        raise ValueError("source must be a non-empty trimmed string")

    if isinstance(raw_msg, bytes):
        try:
            raw_json = raw_msg.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return ParseResult(kind="malformed", reason="invalid_utf8")
    elif isinstance(raw_msg, str):
        raw_json = raw_msg
    else:
        return ParseResult(kind="malformed", reason="unsupported_frame_type")

    try:
        payload = json.loads(raw_json, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return ParseResult(kind="malformed", reason="invalid_json")
    if not isinstance(payload, dict):
        return ParseResult(kind="malformed", reason="root_not_object")

    error = payload.get("error")
    if error is not None and error != {}:
        return ParseResult(
            kind="source_error",
            error_code=_source_error_code(error),
            reason="source_error",
        )

    if payload.get("responseType") not in _TRADE_RESPONSE_TYPES:
        return ParseResult(kind="other", reason="response_type")

    discriminator_count = len(_TRADE_DISCRIMINATORS.intersection(payload))
    if discriminator_count == 0:
        return ParseResult(kind="control", reason="no_trade_discriminators")
    if discriminator_count != len(_TRADE_DISCRIMINATORS):
        return ParseResult(kind="malformed_trade", reason="partial_trade_payload")

    ticker = _identifier(payload.get("ticker"))
    if ticker is None:
        return ParseResult(kind="malformed_trade", reason="invalid_ticker")
    class_code = _identifier(payload.get("classCode"))
    if class_code is None:
        return ParseResult(kind="malformed_trade", reason="invalid_class_code")
    event_time = _parse_event_time(payload.get("dateTime"))
    if event_time is None:
        return ParseResult(kind="malformed_trade", reason="invalid_event_time")

    return ParseResult(
        kind="trade",
        row=TradeRawRow(
            received_at=normalized_received_at,
            source=source,
            ticker=ticker,
            class_code=class_code,
            event_time=event_time,
            raw_json=raw_json,
        ),
    )
