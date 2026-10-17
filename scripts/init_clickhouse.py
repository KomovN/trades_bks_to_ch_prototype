#!/usr/bin/env python3
"""Create the selected database and apply the immutable table DDL once."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys

import clickhouse_connect


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CREATE_TARGET = re.compile(
    r"\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<database>[A-Za-z_][A-Za-z0-9_]*)\.trades_raw_json\s*\(",
    re.IGNORECASE,
)


def _required(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _port() -> int:
    raw = _required("CLICKHOUSE_PORT", "8123")
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("CLICKHOUSE_PORT must be an integer")
    value = int(raw)
    if not 1 <= value <= 65535:
        raise ValueError("CLICKHOUSE_PORT is outside the allowed range")
    return value


def _positive_timeout(name: str, default: str) -> float:
    try:
        value = float(_required(name, default))
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not 0 < value <= 10:
        raise ValueError(f"{name} is outside the allowed range")
    return value


def _secure() -> bool:
    raw = _required("CLICKHOUSE_SECURE", "false")
    if raw not in {"true", "false"}:
        raise ValueError("CLICKHOUSE_SECURE must be exactly 'true' or 'false'")
    return raw == "true"


def _client(database: str):
    return clickhouse_connect.get_client(
        host=_required("CLICKHOUSE_HOST", "127.0.0.1"),
        port=_port(),
        username=_required("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=database,
        secure=_secure(),
        connect_timeout=_positive_timeout("CLICKHOUSE_CONNECT_TIMEOUT_SEC", "5"),
        send_receive_timeout=_positive_timeout("CLICKHOUSE_IO_TIMEOUT_SEC", "10"),
        query_retries=0,
        show_clickhouse_errors="scrub",
    )


def initialize(ddl_path: Path) -> tuple[str, str]:
    database = _required("CLICKHOUSE_DATABASE", "bronze")
    if not _IDENTIFIER.fullmatch(database):
        raise ValueError("CLICKHOUSE_DATABASE must be a simple identifier")
    ddl = ddl_path.read_text(encoding="utf-8")
    if not ddl.strip():
        raise ValueError("DDL file is empty")
    target_name = _CREATE_TARGET.match(ddl.lstrip())
    if target_name is None:
        raise ValueError("DDL must create a database-qualified trades_raw_json table")
    if target_name.group("database") != database:
        raise ValueError("CLICKHOUSE_DATABASE must match the database in the DDL")

    # Validate the migration target before opening a client or creating anything.
    bootstrap = _client("default")
    try:
        bootstrap.command(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    finally:
        bootstrap.close()

    target = _client(database)
    try:
        target.command(ddl)
        version = str(target.command("SELECT version()"))
        engine = str(
            target.command(
                "SELECT engine FROM system.tables "
                "WHERE database = currentDatabase() AND name = 'trades_raw_json'"
            )
        )
    finally:
        target.close()
    if engine != "MergeTree":
        raise RuntimeError("initialized table has an unexpected engine")
    return database, version


def inspect() -> tuple[str, str, str, int, object]:
    database = _required("CLICKHOUSE_DATABASE", "bronze")
    if not _IDENTIFIER.fullmatch(database):
        raise ValueError("CLICKHOUSE_DATABASE must be a simple identifier")
    client = _client(database)
    try:
        version, timezone_name = client.query("SELECT version(), timezone()").first_row
        create_table = str(client.command("SHOW CREATE TABLE trades_raw_json"))
        count, latest = client.query(
            "SELECT count(), max(received_at) FROM trades_raw_json"
        ).first_row
    finally:
        client.close()
    return str(version), str(timezone_name), create_table, int(count), latest


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        try:
            version, timezone_name, create_table, count, latest = inspect()
        except Exception as error:
            print(f"ClickHouse check failed: {type(error).__name__}", file=sys.stderr)
            return 1
        print(f"version={version} timezone={timezone_name}")
        print(create_table)
        print(f"rows={count} max_received_at={latest}")
        return 0
    if len(args) != 1:
        print(
            "usage: init_clickhouse.py PATH_TO_DDL | --check",
            file=sys.stderr,
        )
        return 2
    try:
        database, version = initialize(Path(args[0]))
    except Exception as error:
        print(f"ClickHouse init failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(
        f"ClickHouse init complete: database={database} "
        f"table=trades_raw_json version={version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
