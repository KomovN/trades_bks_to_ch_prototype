from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.parser import ParseResult, TradeRawRow, parse_message


ROOT = Path(__file__).resolve().parents[1]
FIXTURES_PATH = ROOT / "docs" / "fixtures" / "messages.json"
RECEIVED_AT = datetime(2026, 10, 17, 12, 34, 56, 987654, tzinfo=timezone.utc)


def _fixture_cases() -> list[dict[str, object]]:
    document = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return document["cases"]


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: case["name"])
def test_contract_fixtures(case: dict[str, object]) -> None:
    raw = (
        bytes.fromhex(str(case["input_hex"]))
        if "input_hex" in case
        else str(case["raw"])
    )

    result = parse_message(raw, source="bks", received_at=RECEIVED_AT)

    assert result.kind == case["expected_kind"]
    if result.kind == "trade":
        assert result.row is not None
        assert result.row.raw_json == raw
        if "expected_event_time" in case:
            expected = datetime.fromisoformat(
                str(case["expected_event_time"]).replace("Z", "+00:00")
            )
            assert result.row.event_time == expected
    else:
        assert result.row is None


def test_raw_json_and_bronze_metadata_are_exact() -> None:
    raw = (
        '{ "responseType": "LastTrades", "ticker": "sBeR", '
        '"classCode": "TQBR", "side": null, "price": 316.5300, '
        '"quantity": {"future": true}, "dateTime": '
        '"2026-03-13T15:09:45.123999Z", "escaped": "\\u042f" }'
    )
    received_at = datetime(
        2026, 10, 17, 15, 34, 56, 987654, tzinfo=timezone(timedelta(hours=3))
    )

    result = parse_message(raw, source="bks", received_at=received_at)

    assert result == ParseResult(
        kind="trade",
        row=TradeRawRow(
            received_at=datetime(2026, 10, 17, 12, 34, 56, 987000, timezone.utc),
            source="bks",
            ticker="sBeR",
            class_code="TQBR",
            event_time=datetime(2026, 3, 13, 15, 9, 45, 123000, timezone.utc),
            raw_json=raw,
        ),
    )
    assert "316.5300" in result.row.raw_json
    assert '"escaped": "\\u042f"' in result.row.raw_json


def test_valid_utf8_bytes_preserve_decoded_text_exactly() -> None:
    raw = (
        '{"responseType":"LastTrades","ticker":"SBER","classCode":"TQBR",'
        '"side":"BUY","price":"316.5300","quantity":1,'
        '"dateTime":"2026-03-13T15:09:45.123Z","note":"Я"}'
    )

    result = parse_message(raw.encode("utf-8"), source="bks", received_at=RECEIVED_AT)

    assert result.kind == "trade"
    assert result.row is not None
    assert result.row.raw_json == raw


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_json_constants_are_malformed(constant: str) -> None:
    raw = (
        '{"responseType":"LastTrades","ticker":"SBER","classCode":"TQBR",'
        f'"side":"BUY","price":{constant},"quantity":1,'
        '"dateTime":"2026-03-13T15:09:45.123Z"}'
    )

    result = parse_message(raw, source="bks", received_at=RECEIVED_AT)

    assert result == ParseResult(kind="malformed", reason="invalid_json")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("ticker", '""', "invalid_ticker"),
        ("ticker", '" SBER"', "invalid_ticker"),
        ("classCode", '"TQBR "', "invalid_class_code"),
        ("classCode", "false", "invalid_class_code"),
        ("dateTime", '"not-a-time"', "invalid_event_time"),
        ("dateTime", '"2026-03-13T15:09:45.123z"', "invalid_event_time"),
    ],
)
def test_invalid_trade_metadata_is_rejected(
    field: str, value: str, reason: str
) -> None:
    payload = {
        "responseType": "LastTrades",
        "ticker": "SBER",
        "classCode": "TQBR",
        "side": "BUY",
        "price": 1,
        "quantity": 1,
        "dateTime": "2026-03-13T15:09:45.123Z",
    }
    raw = json.dumps(payload, separators=(",", ":")).replace(
        json.dumps(payload[field]), value, 1
    )

    result = parse_message(raw, source="bks", received_at=RECEIVED_AT)

    assert result == ParseResult(kind="malformed_trade", reason=reason)


def test_missing_event_time_is_rejected_without_default_now() -> None:
    raw = (
        '{"responseType":"LastTrades","ticker":"SBER","classCode":"TQBR",'
        '"side":"BUY","price":1,"quantity":1}'
    )

    result = parse_message(raw, source="bks", received_at=RECEIVED_AT)

    assert result == ParseResult(
        kind="malformed_trade", reason="invalid_event_time"
    )


@pytest.mark.parametrize("error", ["null", "{}"])
def test_empty_error_does_not_override_trade(error: str) -> None:
    raw = (
        '{"responseType":"LastTrades","ticker":"SBER","classCode":"TQBR",'
        '"side":"BUY","price":1,"quantity":1,'
        f'"dateTime":"2026-03-13T15:09:45.123Z","error":{error}}}'
    )

    assert parse_message(raw, source="bks", received_at=RECEIVED_AT).kind == "trade"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        ('{"code":2,"message":"NotFound"}', 2),
        ('{"code":"2"}', None),
        ('{"code":true}', None),
        ('{"code":[2]}', None),
        ('"upstream failure"', None),
    ],
)
def test_source_error_code_is_structured_and_not_coerced(
    error: str, expected_code: int | None
) -> None:
    raw = (
        '{"responseType":"LastTrades","ticker":"SBER","classCode":"TQBR",'
        '"side":"BUY","price":1,"quantity":1,'
        f'"dateTime":"2026-03-13T15:09:45.123Z","error":{error}}}'
    )

    result = parse_message(raw, source="bks", received_at=RECEIVED_AT)

    assert result.kind == "source_error"
    assert result.row is None
    assert result.error_code == expected_code


@pytest.mark.parametrize(
    "received_at",
    [datetime(2026, 10, 17, 12, 0), "2026-10-17T12:00:00Z"],
)
def test_received_at_must_be_an_aware_datetime(received_at: object) -> None:
    with pytest.raises(ValueError, match="received_at"):
        parse_message("{}", source="bks", received_at=received_at)  # type: ignore[arg-type]


@pytest.mark.parametrize("source", ["", " bks", "bks ", 1])
def test_source_must_be_a_trimmed_nonempty_string(source: object) -> None:
    with pytest.raises(ValueError, match="source"):
        parse_message(
            "{}", source=source, received_at=RECEIVED_AT  # type: ignore[arg-type]
        )


def test_result_and_row_are_immutable() -> None:
    raw = (
        '{"responseType":"LastTrades","ticker":"SBER","classCode":"TQBR",'
        '"side":"BUY","price":1,"quantity":1,'
        '"dateTime":"2026-03-13T15:09:45.123Z"}'
    )
    result = parse_message(raw, source="bks", received_at=RECEIVED_AT)
    assert result.row is not None

    with pytest.raises(FrozenInstanceError):
        result.kind = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.row.ticker = "GAZP"  # type: ignore[misc]
