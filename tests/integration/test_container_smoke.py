from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid

import pytest
from websockets.asyncio.server import serve

from tests.integration.helpers import clickhouse_client


pytestmark = pytest.mark.integration


def test_container_ingests_from_fake_authenticated_websocket() -> None:
    asyncio.run(_container_smoke())


async def _container_smoke() -> None:
    token = "container-smoke-fake-token"
    marker = str(uuid.uuid4())
    raw_json = (
        '{"responseType":"LastTrades","ticker":"SMOKE","classCode":"TQBR",'
        '"side":"S","price":316.5300,"quantity":1,'
        '"dateTime":"2026-10-17T15:00:00.456789+03:00",'
        f'"smokeMarker":"{marker}"}}'
    )
    validated: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def handler(websocket) -> None:
        try:
            assert websocket.request.headers["Authorization"] == f"Bearer {token}"
            subscription = json.loads(await websocket.recv())
            assert subscription["dataType"] == 2
            assert subscription["subscribeType"] == 0
            assert len(subscription["instruments"]) == 101
            if not validated.done():
                validated.set_result(None)
            await websocket.send(raw_json)
            await websocket.wait_closed()
        except BaseException as error:
            if not validated.done():
                validated.set_exception(error)
            raise

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        child_env = os.environ.copy()
        child_env.update(
            {
                "WS_URL": f"ws://127.0.0.1:{port}/market-data",
                "SOURCE": "bks",
                "SUBSCRIBE_CONFIG": "/app/config/subscribe_msg.yaml",
                "ACCESS_TOKEN": token,
                "REFRESH_TOKEN": "",
                "ACCESS_TOKEN_MAX_AGE_SEC": "82800",
                "AUTH_FAILED_EXIT_DELAY_SEC": "0",
                "CLICKHOUSE_HOST": os.environ["INTEGRATION_CLICKHOUSE_HOST"],
                "CLICKHOUSE_TABLE": "trades_raw_json",
                "BATCH_SIZE": "1",
                "FLUSH_INTERVAL_SEC": "0.1",
                "QUEUE_MAXSIZE": "10",
                "RECONNECT_MIN_DELAY_SEC": "0.05",
                "RECONNECT_MAX_DELAY_SEC": "0.1",
                "SHUTDOWN_TIMEOUT_SEC": "15",
                "LOG_LEVEL": "INFO",
                "LOG_SUMMARY_INTERVAL_SEC": "60",
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.main",
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_bytes = b""
        try:
            await asyncio.wait_for(validated, timeout=5)

            def row_count() -> int:
                client = clickhouse_client()
                try:
                    return int(
                        client.command(
                            "SELECT count() FROM trades_raw_json "
                            "WHERE raw_json = {raw:String}",
                            parameters={"raw": raw_json},
                        )
                    )
                finally:
                    client.close()

            deadline = asyncio.get_running_loop().time() + 10
            while await asyncio.to_thread(row_count) != 1:
                if process.returncode is not None:
                    raise AssertionError("service exited before inserting the fake trade")
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("service did not insert the fake trade in time")
                await asyncio.sleep(0.1)
            process.send_signal(signal.SIGTERM)
            output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=20)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    output = output_bytes.decode("utf-8", errors="replace")
    assert process.returncode == 0, output
    assert "event=service_exit code=0" in output
    assert "accepted_trades=1 inserted_rows=1 unsaved_rows=0" in output
    assert token not in output
