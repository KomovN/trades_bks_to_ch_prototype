from __future__ import annotations

import os

import clickhouse_connect


def integration_host() -> str:
    host = os.environ.get("INTEGRATION_CLICKHOUSE_HOST")
    if not host:
        raise RuntimeError(
            "INTEGRATION_CLICKHOUSE_HOST is required; run scripts/run_integration.sh"
        )
    return host


def clickhouse_client(*, database: str | None = None):
    return clickhouse_connect.get_client(
        host=integration_host(),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "integration"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=database or os.environ.get("CLICKHOUSE_DATABASE", "trades_integration"),
        secure=False,
        connect_timeout=5,
        send_receive_timeout=10,
        query_retries=0,
        show_clickhouse_errors="scrub",
    )


def integration_database() -> str:
    return os.environ.get("CLICKHOUSE_DATABASE", "trades_integration")
