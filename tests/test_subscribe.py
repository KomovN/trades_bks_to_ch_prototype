from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.subscribe import (
    EXPECTED_INSTRUMENT_COUNT,
    EXPECTED_PAIRS_SHA256,
    SubscriptionError,
    load_subscription,
    parse_subscription,
)


ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION_PATH = ROOT / "subscribe_msg.yaml"


def test_agreed_subscription_snapshot_and_wire_json() -> None:
    subscription = load_subscription(SUBSCRIPTION_PATH)

    assert subscription.subscribe_type == 0
    assert subscription.data_type == 2
    assert len(subscription.instruments) == EXPECTED_INSTRUMENT_COUNT == 101
    pairs = [(item.ticker, item.class_code) for item in subscription.instruments]
    assert len(pairs) == len(set(pairs))
    digest = hashlib.sha256(
        "".join(f"{ticker}|{class_code}\n" for ticker, class_code in pairs).encode()
    ).hexdigest()
    assert digest == EXPECTED_PAIRS_SHA256
    assert ("T", "TQBR") in pairs

    wire = json.loads(subscription.to_json())
    assert list(wire) == ["subscribeType", "dataType", "instruments"]
    assert wire["instruments"] == [
        {"ticker": ticker, "classCode": class_code} for ticker, class_code in pairs
    ]


@pytest.mark.parametrize(
    "text",
    [
        "subscribeType: 0\nsubscribeType: 0\ndataType: 2\ninstruments: []\n",
        (
            "subscribeType: 0\ndataType: 2\ninstruments:\n"
            "  - ticker: SBER\n    ticker: GAZP\n    classCode: TQBR\n"
        ),
    ],
)
def test_duplicate_yaml_keys_are_rejected(text: str) -> None:
    with pytest.raises(SubscriptionError, match="YAML is invalid"):
        parse_subscription(text)


@pytest.mark.parametrize(
    "text",
    [
        "subscribeType: 0\ndataType: 2\ninstruments: []\nextra: true\n",
        (
            "subscribeType: 0\ndataType: 2\ninstruments:\n"
            "  - ticker: SBER\n    classCode: TQBR\n    extra: true\n"
        ),
    ],
)
def test_unknown_fields_are_rejected(text: str) -> None:
    with pytest.raises(SubscriptionError, match="missing or unknown fields"):
        parse_subscription(text)


@pytest.mark.parametrize(
    "text",
    [
        "subscribeType: false\ndataType: 2\ninstruments: []\n",
        "subscribeType: 0\ndataType: '2'\ninstruments: []\n",
        "subscribeType: 0\ndataType: 2\ninstruments: {}\n",
        (
            "subscribeType: 0\ndataType: 2\ninstruments:\n"
            "  - ticker: 123\n    classCode: TQBR\n"
        ),
    ],
)
def test_wrong_types_are_rejected(text: str) -> None:
    with pytest.raises(SubscriptionError):
        parse_subscription(text)


def test_empty_instruments_are_rejected() -> None:
    with pytest.raises(SubscriptionError, match="non-empty list"):
        parse_subscription("subscribeType: 0\ndataType: 2\ninstruments: []\n")


def test_duplicate_pairs_are_rejected() -> None:
    text = (
        "subscribeType: 0\ndataType: 2\ninstruments:\n"
        "  - ticker: SBER\n    classCode: TQBR\n"
        "  - ticker: SBER\n    classCode: TQBR\n"
    )
    with pytest.raises(SubscriptionError, match="duplicate pairs"):
        parse_subscription(text)


def test_snapshot_change_is_rejected_without_case_normalization() -> None:
    text = SUBSCRIPTION_PATH.read_text(encoding="utf-8").replace(
        'ticker: "SBER"', 'ticker: "sber"', 1
    )

    with pytest.raises(SubscriptionError, match="agreed snapshot"):
        parse_subscription(text)


def test_unsafe_yaml_tag_is_rejected() -> None:
    with pytest.raises(SubscriptionError, match="YAML is invalid"):
        parse_subscription("!!python/object/apply:os.system ['echo unsafe']")


def test_unreadable_file_has_safe_error(tmp_path: Path) -> None:
    with pytest.raises(SubscriptionError, match="cannot be read"):
        load_subscription(tmp_path / "missing.yaml")
