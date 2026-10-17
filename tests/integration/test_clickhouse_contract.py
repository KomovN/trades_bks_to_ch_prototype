from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from app.ch_writer import INSERT_COLUMNS
from tests.integration.helpers import clickhouse_client, integration_database


pytestmark = pytest.mark.integration


def test_s15_real_clickhouse_schema_and_exact_roundtrip() -> None:
    ddl = Path("migrations/001_create_trades_raw_json.sql").read_text(encoding="utf-8")
    database = integration_database()
    client = clickhouse_client()
    try:
        # Проверяем идемпотентность канонического DDL независимо от init-контейнера.
        client.command(ddl)
        client.command(ddl)

        table = client.query(
            "SELECT engine, partition_key, sorting_key, primary_key "
            "FROM system.tables "
            "WHERE database = {database:String} AND name = 'trades_raw_json'",
            parameters={"database": database},
        ).first_row
        assert table == (
            "MergeTree",
            "toYYYYMM(received_at)",
            "ticker, classCode, event_time, received_at",
            "ticker, classCode, event_time",
        )

        columns = {
            row[0]: (row[1], row[2], row[3], row[4])
            for row in client.query(
                "SELECT name, type, default_kind, default_expression, compression_codec "
                "FROM system.columns "
                "WHERE database = {database:String} AND table = 'trades_raw_json'",
                parameters={"database": database},
            ).result_rows
        }
        assert columns["received_at"][0] == "DateTime64(3, 'UTC')"
        assert columns["ticker"][0] == "LowCardinality(String)"
        assert columns["classCode"][0] == "LowCardinality(String)"
        assert columns["event_time"][0] == "DateTime64(3, 'UTC')"
        assert columns["raw_json"][0] == "String"
        assert "Delta(8)" in columns["received_at"][3]
        assert "ZSTD(1)" in columns["received_at"][3]
        assert "Delta(8)" in columns["event_time"][3]
        assert "ZSTD(1)" in columns["event_time"][3]
        assert "ZSTD(1)" in columns["raw_json"][3]

        marker = str(uuid.uuid4())
        raw_json = (
            '{  "responseType":"LastTrades", "ticker":"S15", '
            '"classCode":"TQBR", "side":"B", "price":316.5300, '
            '"quantity":2, "dateTime":"2026-10-17T12:34:55.123987Z", '
            f'"unknown":"{marker}\\u0020exact"  }}'
        )
        received_at = datetime(2026, 10, 17, 12, 34, 56, 789654, tzinfo=timezone.utc)
        event_time = datetime.fromisoformat("2026-10-17T15:34:55.123987+03:00")
        row = (received_at, "bks", "S15", "TQBR", event_time, raw_json)
        client.insert(
            "trades_raw_json",
            [row, row],
            column_names=INSERT_COLUMNS,
        )

        stored = client.query(
            "SELECT raw_json, toUnixTimestamp64Milli(received_at), "
            "toUnixTimestamp64Milli(event_time) FROM trades_raw_json "
            "WHERE raw_json = {raw:String}",
            parameters={"raw": raw_json},
        ).result_rows
        assert len(stored) == 2
        expected_received_ms = int(received_at.timestamp() * 1000)
        expected_event_ms = int(event_time.timestamp() * 1000)
        assert stored == [
            (raw_json, expected_received_ms, expected_event_ms),
            (raw_json, expected_received_ms, expected_event_ms),
        ]
    finally:
        client.close()


def test_check_cli_reports_safe_current_metadata() -> None:
    result = subprocess.run(
        [sys.executable, "/app/scripts/init_clickhouse.py", "--check"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "version=" in result.stdout
    assert " timezone=" in result.stdout
    assert "CREATE TABLE" in result.stdout
    assert "trades_raw_json" in result.stdout
    assert "rows=" in result.stdout
    assert "max_received_at=" in result.stdout
    assert os.environ["CLICKHOUSE_PASSWORD"] not in result.stdout
    assert os.environ["CLICKHOUSE_PASSWORD"] not in result.stderr
