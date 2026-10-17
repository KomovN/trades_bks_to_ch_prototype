from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run_runner(tmp_path: Path, failure: str = "") -> tuple[int, list[list[str]]]:
    """Run the real shell lifecycle against a harmless Docker CLI substitute."""

    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CALL_LOG'], 'a') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "failure = os.environ.get('FAKE_FAILURE')\n"
        "if failure == 'integration' and args[-3:] == ['run', '--rm', 'integration']:\n"
        "    sys.exit(42)\n"
        "if failure == 'cleanup' and args[-2:] == ['down', '--remove-orphans']:\n"
        "    sys.exit(43)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    call_log = tmp_path / "calls.jsonl"
    env = os.environ.copy()
    env.update(
        PATH=f"{tmp_path}{os.pathsep}{env['PATH']}",
        CALL_LOG=str(call_log),
        FAKE_FAILURE=failure,
        COMPOSE_PROJECT_NAME="quotes-production",
        COMPOSE_FILE="production.yml",
    )
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/run_integration.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    return result.returncode, calls


def test_integration_cleanup_cannot_inherit_production_project(tmp_path: Path) -> None:
    return_code, calls = run_runner(tmp_path)

    assert return_code == 0
    assert calls
    for args in calls:
        assert args[:7] == [
            "compose", "--env-file", str(ROOT / "tests/integration/compose-safe.env"),
            "-p", "trades-bks-to-ch-integration", "-f",
            str(ROOT / "docker-compose.local.yml"),
        ]
        assert "-v" not in args
    assert calls[-1][-2:] == ["down", "--remove-orphans"]


def test_failed_integration_still_cleans_up_and_preserves_failure(tmp_path: Path) -> None:
    return_code, calls = run_runner(tmp_path, "integration")

    assert return_code == 42
    assert calls[-1][-2:] == ["down", "--remove-orphans"]
    assert not any(args[-3:] == ["run", "--rm", "container-smoke"] for args in calls)


def test_failed_cleanup_does_not_report_a_successful_run(tmp_path: Path) -> None:
    return_code, calls = run_runner(tmp_path, "cleanup")

    assert return_code == 1
    assert calls[-1][-2:] == ["down", "--remove-orphans"]
