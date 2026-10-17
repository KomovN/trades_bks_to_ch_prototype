from __future__ import annotations

from pathlib import Path

import pytest

from scripts import init_clickhouse


MIGRATION = Path(__file__).resolve().parents[1] / "migrations/001_create_trades_raw_json.sql"


@pytest.mark.parametrize("configured_database", [None, "bronze"])
def test_init_creates_the_database_used_by_the_canonical_ddl(
    monkeypatch: pytest.MonkeyPatch, configured_database: str | None
) -> None:
    if configured_database is None:
        monkeypatch.delenv("CLICKHOUSE_DATABASE", raising=False)
    else:
        monkeypatch.setenv("CLICKHOUSE_DATABASE", configured_database)
    clients: list[tuple[str, FakeClient]] = []

    def client_factory(database: str) -> FakeClient:
        client = FakeClient()
        clients.append((database, client))
        return client

    monkeypatch.setattr(init_clickhouse, "_client", client_factory)

    assert init_clickhouse.initialize(MIGRATION) == ("bronze", "24.8.4.13")
    assert [database for database, _ in clients] == ["default", "bronze"]
    assert clients[0][1].commands == ["CREATE DATABASE IF NOT EXISTS `bronze`"]
    assert clients[1][1].commands[0] == MIGRATION.read_text(encoding="utf-8")
    assert all(client.closed for _, client in clients)


def test_database_mismatch_is_rejected_before_any_client_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "default")
    monkeypatch.setattr(init_clickhouse, "_client", forbidden_client)

    with pytest.raises(ValueError, match="CLICKHOUSE_DATABASE must match"):
        init_clickhouse.initialize(MIGRATION)


@pytest.mark.parametrize("target", ["trades_raw_json", "bronze.trades_raw_json_other"])
def test_unexpected_migration_target_is_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    migration = tmp_path / "migration.sql"
    migration.write_text(
        f"CREATE TABLE IF NOT EXISTS {target} (value String) ENGINE = MergeTree ORDER BY tuple();",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "bronze")
    monkeypatch.setattr(init_clickhouse, "_client", forbidden_client)

    with pytest.raises(ValueError, match="database-qualified trades_raw_json"):
        init_clickhouse.initialize(migration)


def forbidden_client(database: str):
    pytest.fail("configuration error must be rejected before opening a client")


class FakeClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.closed = False

    def command(self, sql: str):
        self.commands.append(sql)
        if sql == "SELECT version()":
            return "24.8.4.13"
        if sql.startswith("SELECT engine FROM system.tables"):
            return "MergeTree"
        return None

    def close(self) -> None:
        self.closed = True
