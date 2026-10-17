"""Безопасная загрузка фиксированного wire-контракта подписки БКС."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


EXPECTED_INSTRUMENT_COUNT = 101
EXPECTED_PAIRS_SHA256 = "b9e8f030630ed3cca638940620dfbd4fed74be9761d922f22eb38d19092fb130"


class SubscriptionError(ValueError):
    """Ошибка структуры или согласованного содержимого подписки."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True, slots=True)
class Instrument:
    ticker: str
    class_code: str

    def to_wire(self) -> dict[str, str]:
        return {"ticker": self.ticker, "classCode": self.class_code}


@dataclass(frozen=True, slots=True)
class Subscription:
    subscribe_type: int
    data_type: int
    instruments: tuple[Instrument, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "subscribeType": self.subscribe_type,
            "dataType": self.data_type,
            "instruments": [instrument.to_wire() for instrument in self.instruments],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_wire(), ensure_ascii=False, separators=(",", ":"))


def _require_exact_keys(value: dict[Any, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        raise SubscriptionError(f"{location} has missing or unknown fields")


def _instrument(value: Any, index: int) -> Instrument:
    if type(value) is not dict:
        raise SubscriptionError(f"instruments[{index}] must be an object")
    _require_exact_keys(value, {"ticker", "classCode"}, f"instruments[{index}]")
    ticker = value["ticker"]
    class_code = value["classCode"]
    if (
        type(ticker) is not str
        or not ticker
        or ticker != ticker.strip()
        or type(class_code) is not str
        or not class_code
        or class_code != class_code.strip()
    ):
        raise SubscriptionError(
            f"instruments[{index}] ticker and classCode must be non-empty strings"
        )
    return Instrument(ticker=ticker, class_code=class_code)


def _pairs_digest(instruments: tuple[Instrument, ...]) -> str:
    canonical = "".join(
        f"{instrument.ticker}|{instrument.class_code}\n" for instrument in instruments
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_subscription(text: str) -> Subscription:
    """Разобрать YAML и проверить точный согласованный snapshot инструментов."""

    try:
        data = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError:
        raise SubscriptionError("subscription YAML is invalid") from None
    if type(data) is not dict:
        raise SubscriptionError("subscription root must be an object")
    _require_exact_keys(data, {"subscribeType", "dataType", "instruments"}, "root")
    if type(data["subscribeType"]) is not int or data["subscribeType"] != 0:
        raise SubscriptionError("subscribeType must be integer 0")
    if type(data["dataType"]) is not int or data["dataType"] != 2:
        raise SubscriptionError("dataType must be integer 2")
    raw_instruments = data["instruments"]
    if type(raw_instruments) is not list or not raw_instruments:
        raise SubscriptionError("instruments must be a non-empty list")
    instruments = tuple(
        _instrument(value, index) for index, value in enumerate(raw_instruments)
    )
    pairs = [(item.ticker, item.class_code) for item in instruments]
    if len(pairs) != len(set(pairs)):
        raise SubscriptionError("instruments must not contain duplicate pairs")
    if (
        len(instruments) != EXPECTED_INSTRUMENT_COUNT
        or _pairs_digest(instruments) != EXPECTED_PAIRS_SHA256
    ):
        raise SubscriptionError("instruments differ from the agreed snapshot")
    return Subscription(subscribe_type=0, data_type=2, instruments=instruments)


def load_subscription(path: str | Path) -> Subscription:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise SubscriptionError("subscription file cannot be read as UTF-8") from None
    return parse_subscription(text)
