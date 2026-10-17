"""Загрузка и строгая проверка конфигурации до запуска сетевых клиентов."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values


DEFAULT_WS_URL = (
    "wss://ws.broker.ru/trade-api-market-data-connector/api/v1/market-data/ws"
)

_DEFAULTS: dict[str, str] = {
    "WS_URL": DEFAULT_WS_URL,
    "SOURCE": "bks",
    "SUBSCRIBE_CONFIG": "subscribe_msg.yaml",
    "ACCESS_TOKEN": "",
    "REFRESH_TOKEN": "",
    "ACCESS_TOKEN_MAX_AGE_SEC": "82800",
    "AUTH_FAILED_EXIT_DELAY_SEC": "600",
    "CLICKHOUSE_HOST": "127.0.0.1",
    "CLICKHOUSE_PORT": "8123",
    "CLICKHOUSE_USER": "default",
    "CLICKHOUSE_PASSWORD": "",
    "CLICKHOUSE_DATABASE": "bronze",
    "CLICKHOUSE_TABLE": "trades_raw_json",
    "CLICKHOUSE_SECURE": "false",
    "CLICKHOUSE_CONNECT_TIMEOUT_SEC": "5",
    "CLICKHOUSE_IO_TIMEOUT_SEC": "10",
    "BATCH_SIZE": "1000",
    "FLUSH_INTERVAL_SEC": "1.0",
    "QUEUE_MAXSIZE": "10000",
    "RECONNECT_MIN_DELAY_SEC": "1",
    "RECONNECT_MAX_DELAY_SEC": "60",
    "SHUTDOWN_TIMEOUT_SEC": "30",
    "LOG_LEVEL": "INFO",
    "LOG_SUMMARY_INTERVAL_SEC": "60",
}

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_UNSIGNED_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class SettingsError(ValueError):
    """Безопасная для вывода ошибка конфигурации без значений env."""


def _text(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key, _DEFAULTS[key])
    if not isinstance(value, str):
        raise SettingsError(f"{key} must be a string")
    return value


def _nonempty(values: Mapping[str, Any], key: str) -> str:
    value = _text(values, key)
    if not value or value != value.strip():
        raise SettingsError(f"{key} must be a non-empty trimmed string")
    return value


def _secret(values: Mapping[str, Any], key: str) -> str | None:
    value = _text(values, key)
    if not value:
        return None
    if value != value.strip():
        raise SettingsError(f"{key} must not contain surrounding whitespace")
    return value


def _integer(
    values: Mapping[str, Any], key: str, *, minimum: int, maximum: int | None = None
) -> int:
    raw = _text(values, key)
    if not _UNSIGNED_INTEGER.fullmatch(raw):
        raise SettingsError(f"{key} must be an integer")
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        raise SettingsError(f"{key} is outside the allowed range")
    return value


def _finite_float(
    values: Mapping[str, Any], key: str, *, minimum: float, allow_equal: bool = False
) -> float:
    raw = _text(values, key)
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{key} must be a finite number") from None
    if not math.isfinite(value):
        raise SettingsError(f"{key} must be a finite number")
    valid = value >= minimum if allow_equal else value > minimum
    if not valid:
        raise SettingsError(f"{key} is outside the allowed range")
    return value


def _strict_bool(values: Mapping[str, Any], key: str) -> bool:
    raw = _text(values, key)
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise SettingsError(f"{key} must be exactly 'true' or 'false'")


def _ws_url(values: Mapping[str, Any]) -> str:
    raw = _nonempty(values, "WS_URL")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SettingsError("WS_URL must be a WebSocket URL without credentials or fragment")
    try:
        parsed.port
    except ValueError:
        raise SettingsError("WS_URL has an invalid port") from None
    if parsed.scheme == "ws":
        hostname = parsed.hostname
        is_loopback = hostname == "localhost"
        if not is_loopback:
            try:
                is_loopback = ip_address(hostname).is_loopback
            except ValueError:
                pass
        if not is_loopback:
            raise SettingsError("WS_URL permits plain ws only for a loopback host")
    return raw


def _clickhouse_host(values: Mapping[str, Any]) -> str:
    host = _nonempty(values, "CLICKHOUSE_HOST")
    if any(char.isspace() for char in host) or any(
        marker in host for marker in ("://", "@", "/", "?", "#")
    ):
        raise SettingsError("CLICKHOUSE_HOST must be a host without URL or credentials")
    if ":" in host:
        try:
            ip_address(host)
        except ValueError:
            raise SettingsError(
                "CLICKHOUSE_HOST must not include a port; use CLICKHOUSE_PORT"
            ) from None
    return host


@dataclass(frozen=True, slots=True)
class Settings:
    ws_url: str
    source: str
    subscribe_config: Path
    access_token: str | None = field(repr=False)
    refresh_token: str | None = field(repr=False)
    access_token_max_age_sec: float
    auth_failed_exit_delay_sec: float
    clickhouse_host: str
    clickhouse_port: int
    clickhouse_user: str
    clickhouse_password: str | None = field(repr=False)
    clickhouse_database: str
    clickhouse_table: str
    clickhouse_secure: bool
    clickhouse_connect_timeout_sec: float
    clickhouse_io_timeout_sec: float
    batch_size: int
    flush_interval_sec: float
    queue_maxsize: int
    reconnect_min_delay_sec: float
    reconnect_max_delay_sec: float
    shutdown_timeout_sec: float
    log_level: str
    log_summary_interval_sec: float

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, Any] | None = None,
        *,
        base_dir: str | Path | None = None,
    ) -> Settings:
        values: Mapping[str, Any] = os.environ if env is None else env
        root = Path.cwd() if base_dir is None else Path(base_dir)

        access_token = _secret(values, "ACCESS_TOKEN")
        refresh_token = _secret(values, "REFRESH_TOKEN")
        if access_token is None and refresh_token is None:
            raise SettingsError("ACCESS_TOKEN or REFRESH_TOKEN must be configured")

        config_raw = _nonempty(values, "SUBSCRIBE_CONFIG")
        config_path = Path(config_raw)
        if not config_path.is_absolute():
            config_path = root / config_path
        config_path = config_path.resolve()
        if not config_path.is_file():
            raise SettingsError("SUBSCRIBE_CONFIG must point to an existing file")

        database = _nonempty(values, "CLICKHOUSE_DATABASE")
        if not _IDENTIFIER.fullmatch(database):
            raise SettingsError("CLICKHOUSE_DATABASE must be a simple identifier")
        table = _nonempty(values, "CLICKHOUSE_TABLE")
        if table != "trades_raw_json":
            raise SettingsError("CLICKHOUSE_TABLE must be trades_raw_json")

        connect_timeout = _finite_float(
            values, "CLICKHOUSE_CONNECT_TIMEOUT_SEC", minimum=0
        )
        io_timeout = _finite_float(values, "CLICKHOUSE_IO_TIMEOUT_SEC", minimum=0)
        if connect_timeout > 10:
            raise SettingsError("CLICKHOUSE_CONNECT_TIMEOUT_SEC must be <= 10")
        if io_timeout > 10:
            raise SettingsError("CLICKHOUSE_IO_TIMEOUT_SEC must be <= 10")
        if connect_timeout + io_timeout > 15:
            raise SettingsError(
                "CLICKHOUSE_CONNECT_TIMEOUT_SEC + CLICKHOUSE_IO_TIMEOUT_SEC "
                "must be <= 15"
            )

        reconnect_min = _finite_float(values, "RECONNECT_MIN_DELAY_SEC", minimum=0)
        reconnect_max = _finite_float(values, "RECONNECT_MAX_DELAY_SEC", minimum=0)
        if reconnect_max < reconnect_min:
            raise SettingsError(
                "RECONNECT_MAX_DELAY_SEC must be >= RECONNECT_MIN_DELAY_SEC"
            )

        shutdown_timeout = _finite_float(values, "SHUTDOWN_TIMEOUT_SEC", minimum=0)
        if shutdown_timeout <= io_timeout:
            raise SettingsError(
                "SHUTDOWN_TIMEOUT_SEC must be greater than CLICKHOUSE_IO_TIMEOUT_SEC"
            )

        log_level = _nonempty(values, "LOG_LEVEL").upper()
        if log_level not in _LOG_LEVELS:
            raise SettingsError("LOG_LEVEL is not supported")

        return cls(
            ws_url=_ws_url(values),
            source=_nonempty(values, "SOURCE"),
            subscribe_config=config_path,
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_max_age_sec=_finite_float(
                values, "ACCESS_TOKEN_MAX_AGE_SEC", minimum=0
            ),
            auth_failed_exit_delay_sec=_finite_float(
                values,
                "AUTH_FAILED_EXIT_DELAY_SEC",
                minimum=0,
                allow_equal=True,
            ),
            clickhouse_host=_clickhouse_host(values),
            clickhouse_port=_integer(
                values, "CLICKHOUSE_PORT", minimum=1, maximum=65535
            ),
            clickhouse_user=_nonempty(values, "CLICKHOUSE_USER"),
            clickhouse_password=_secret(values, "CLICKHOUSE_PASSWORD"),
            clickhouse_database=database,
            clickhouse_table=table,
            clickhouse_secure=_strict_bool(values, "CLICKHOUSE_SECURE"),
            clickhouse_connect_timeout_sec=connect_timeout,
            clickhouse_io_timeout_sec=io_timeout,
            batch_size=_integer(values, "BATCH_SIZE", minimum=1),
            flush_interval_sec=_finite_float(values, "FLUSH_INTERVAL_SEC", minimum=0),
            queue_maxsize=_integer(values, "QUEUE_MAXSIZE", minimum=1),
            reconnect_min_delay_sec=reconnect_min,
            reconnect_max_delay_sec=reconnect_max,
            shutdown_timeout_sec=shutdown_timeout,
            log_level=log_level,
            log_summary_interval_sec=_finite_float(
                values, "LOG_SUMMARY_INTERVAL_SEC", minimum=0
            ),
        )


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Загрузить необязательный .env, поверх него применить process environment."""

    values: dict[str, Any] = {}
    base_dir = Path.cwd()
    if env_file is not None:
        path = Path(env_file)
        if path.is_file():
            values.update(dotenv_values(path))
            base_dir = path.resolve().parent
    values.update(os.environ)
    return Settings.from_env(values, base_dir=base_dir)
