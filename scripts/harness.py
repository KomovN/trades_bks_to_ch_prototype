#!/usr/bin/env python3
"""Offline integrity checks and task briefs; deliberately no service verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = {f"R{i:02}" for i in range(1, 17)}
SCENARIOS = {f"S{i:02}" for i in range(1, 18)}
STATUSES = {"todo", "in_progress", "blocked", "done"}
SECTIONS = (
    "## Цель", "## Входные документы", "## Файлы",
    "## Требования", "## Приёмка", "## Проверки",
)
REQUIRED_FILES = (
    "AGENTS.md", "README.md", "PLANS.md", "Makefile", ".env.example", ".gitignore",
    ".github/workflows/harness.yml", "subscribe_msg.yaml", "tasks/README.md",
    "tasks/index.json", "docs/index.md", "docs/architecture.md", "docs/acceptance.md",
    "docs/decisions.md", "docs/open-questions.md", "docs/references.md",
    "docs/operations.md", "docs/execution-log.md", "docs/specs/service.md",
    "docs/specs/bks-protocol.md", "docs/specs/data-contract.md",
    "docs/specs/reliability.md", "docs/specs/configuration.md",
    "docs/fixtures/README.md", "docs/fixtures/messages.json",
    "docs/fixtures/subscription-baseline.json", "docs/fixtures/schema-baseline.json",
    "migrations/001_create_trades_raw_json.sql",
)


def read_json(root: Path, relative: str):
    return json.loads((root / relative).read_text(encoding="utf-8"))


def repo_file(root: Path, relative: object) -> bool:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return False
    path = (root / relative).resolve()
    return path.is_relative_to(root.resolve()) and path.is_file()


def load_tasks(root: Path) -> list[dict]:
    data = read_json(root, "tasks/index.json")
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("tasks/index.json: schema_version must be 1")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks or any(not isinstance(t, dict) for t in tasks):
        raise ValueError("tasks/index.json: tasks must be a nonempty list of objects")
    return tasks


def task_errors(root: Path, tasks: list[dict]) -> list[str]:
    errors: list[str] = []
    ids = [t.get("id") for t in tasks]
    if any(not isinstance(i, str) or not re.fullmatch(r"T\d{2}", i) for i in ids):
        return ["Task ids must match Txx"]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate task id")
    by_id = {t["id"]: t for t in tasks}
    covered: set[str] = set()
    for task in tasks:
        task_id = task["id"]
        if task.get("kind") not in {"contract", "implementation", "review"}:
            errors.append(f"{task_id}: invalid task kind")
        if not isinstance(task.get("title"), str) or not task["title"].strip():
            errors.append(f"{task_id}: missing title")
        if not repo_file(root, task.get("file")):
            errors.append(f"{task_id}: missing task document")
        else:
            body = (root / task["file"]).read_text(encoding="utf-8")
            if not body.startswith(f"# {task_id} "):
                errors.append(f"{task_id}: task heading mismatch")
            for section in SECTIONS:
                if section not in body:
                    errors.append(f"{task_id}: missing section {section}")
        status = task.get("status")
        if status not in STATUSES:
            errors.append(f"{task_id}: invalid status")
        if status in {"in_progress", "blocked", "done"} and not task.get("owner"):
            errors.append(f"{task_id}: status requires owner")
        if status == "blocked" and not task.get("blocked_reason"):
            errors.append(f"{task_id}: blocked requires reason")
        if status == "done" and not repo_file(root, task.get("evidence")):
            errors.append(f"{task_id}: done requires an existing evidence file")
        deps = task.get("depends_on")
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            errors.append(f"{task_id}: invalid dependency list")
            continue
        if len(deps) != len(set(deps)):
            errors.append(f"{task_id}: duplicate dependency")
        for dep in deps:
            if dep not in by_id:
                errors.append(f"{task_id}: unknown dependency {dep}")
            elif status in {"in_progress", "done"} and by_id[dep].get("status") != "done":
                errors.append(f"{task_id}: unfinished dependency {dep}")
        for field, allowed in (("requirements", REQUIREMENTS), ("scenarios", SCENARIOS)):
            values = task.get(field)
            if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
                errors.append(f"{task_id}: invalid {field}")
                continue
            if set(values) - allowed or len(values) != len(set(values)):
                errors.append(f"{task_id}: unknown or duplicate {field}")
            if field == "requirements" and task.get("kind") != "review":
                covered.update(values)
    if covered != REQUIREMENTS:
        errors.append(f"Requirement task coverage mismatch: {sorted(REQUIREMENTS - covered)}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            errors.append(f"Dependency cycle at {task_id}")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        deps = by_id[task_id].get("depends_on", [])
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, str) and dep in by_id:
                    visit(dep)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)
    return errors


def link_errors(root: Path) -> list[str]:
    errors = []
    paths = list(root.glob("*.md")) + list((root / "docs").rglob("*.md")) + list((root / "tasks").rglob("*.md"))
    for path in paths:
        body = re.sub(r"(?ms)^```.*?^```[^\n]*", "", path.read_text(encoding="utf-8"))
        for match in re.finditer(r"\[[^\]\n]+\]\((<[^>]+>|[^\s)]+)\)", body):
            target = match.group(1).strip("<>")
            if target.startswith("/") or urlsplit(target).scheme:
                # External URLs and local source materials need not exist in CI.
                continue
            relative = unquote(target.split("#", 1)[0])
            if not relative:
                continue
            destination = (path.parent / relative).resolve()
            if not destination.is_relative_to(root.resolve()) or not destination.exists():
                errors.append(f"{path.relative_to(root)}: broken repo link {target}")
    return errors


def example_errors(root: Path) -> list[str]:
    errors = []
    lines = [s.strip() for s in (root / "subscribe_msg.yaml").read_text().splitlines() if s.strip() and not s.lstrip().startswith("#")]
    if lines[:3] != ["subscribeType: 0", "dataType: 2", "instruments:"] or (len(lines) - 3) % 2:
        return ["Subscription example must use subscribeType=0, dataType=2 and ticker/classCode pairs"]
    pairs = []
    for index in range(3, len(lines), 2):
        ticker = re.fullmatch(r'- ticker: "([A-Za-z0-9_]+)"', lines[index])
        board = re.fullmatch(r'classCode: "([A-Za-z0-9_]+)"', lines[index + 1])
        if not ticker or not board:
            return ["Subscription example has unsupported structure; runtime must use safe YAML parser"]
        pairs.append((ticker[1], board[1]))
    baseline = read_json(root, "docs/fixtures/subscription-baseline.json")
    digest = hashlib.sha256("".join(f"{t}|{c}\n" for t, c in pairs).encode()).hexdigest()
    if len(pairs) != baseline["instrument_count"] or len(set(pairs)) != len(pairs) or digest != baseline["pairs_sha256"]:
        errors.append("Subscription differs from agreed quotes instrument snapshot")
    sql = (root / "migrations/001_create_trades_raw_json.sql").read_text()
    sql_hash = hashlib.sha256(re.sub(r"\s+", " ", sql).strip().encode()).hexdigest()
    if sql_hash != read_json(root, "docs/fixtures/schema-baseline.json")["sha256"]:
        errors.append("DDL differs from user schema baseline")
    env = {}
    for line in (root / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, separator, value = line.partition("=")
            if not separator or key in env:
                errors.append("Invalid or duplicate .env.example entry")
            env[key] = value
    for key in ("ACCESS_TOKEN", "REFRESH_TOKEN", "CLICKHOUSE_PASSWORD"):
        if env.get(key) != "":
            errors.append(f"Example secret must be empty: {key}")
    documented = set(re.findall(r"^\| ([A-Z][A-Z0-9_]+) \|", (root / "docs/specs/configuration.md").read_text(), re.M))
    if set(env) != documented:
        errors.append("Env example keys differ from configuration specification")
    cases = read_json(root, "docs/fixtures/messages.json")
    if not cases.get("provenance", "").startswith("synthetic"):
        errors.append("Message fixtures must declare synthetic provenance")
    names = set()
    kinds = {"trade", "control", "other", "malformed", "malformed_trade", "source_error"}
    for case in cases["cases"]:
        if case["name"] in names or case["expected_kind"] not in kinds:
            errors.append("Invalid or duplicate message fixture")
        names.add(case["name"])
        if ("raw" in case) == ("input_hex" in case):
            errors.append("Message fixture must have exactly one input representation")
        if "input_hex" in case:
            bytes.fromhex(case["input_hex"])
        if case["expected_kind"] == "trade":
            payload = json.loads(case["raw"])
            if not isinstance(payload, dict) or not {"ticker", "classCode", "dateTime", "side", "price", "quantity"} <= payload.keys():
                errors.append("Trade fixture is missing required payload keys")
    return errors


def check(root: Path) -> list[str]:
    missing = [p for p in REQUIRED_FILES if not (root / p).is_file()]
    if missing:
        return [f"Missing file: {p}" for p in missing]
    errors = []
    try:
        tasks = load_tasks(root)
        errors.extend(task_errors(root, tasks))
        errors.extend(link_errors(root))
        errors.extend(example_errors(root))
        spec = (root / "docs/specs/service.md").read_text()
        found = re.findall(r"^\| (R\d{2}) \|", spec, re.M)
        if set(found) != REQUIREMENTS or len(found) != len(REQUIREMENTS):
            errors.append("Service requirement ids must be R01–R16 exactly once")
        rows = re.findall(r"^\| (S\d{2}) \| ([^|]+) \|", (root / "docs/acceptance.md").read_text(), re.M)
        if {row[0] for row in rows} != SCENARIOS or len(rows) != len(SCENARIOS):
            errors.append("Acceptance scenario ids must be S01–S17 exactly once")
        coverage = {r for _, reqs in rows for r in re.findall(r"R\d{2}", reqs)}
        if coverage != REQUIREMENTS:
            errors.append("Acceptance requirement coverage mismatch")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        # Report data error type, never echo a possibly secret-bearing value.
        errors.append(f"Invalid Harness data ({type(exc).__name__})")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "tasks", "prompt"))
    parser.add_argument("task_id", nargs="?")
    args = parser.parse_args()
    errors = check(ROOT)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    tasks = load_tasks(ROOT)
    by_id = {t["id"]: t for t in tasks}
    if args.command == "check":
        print(f"PASS: Harness integrity; {len(tasks)} tasks, 16 requirements, 17 acceptance scenarios.")
        print("This target checks Harness metadata only; run `make verify` for runtime and local ClickHouse verification.")
    elif args.command == "tasks":
        for task in tasks:
            pending = [d for d in task["depends_on"] if by_id[d]["status"] != "done"]
            state = task["status"]
            if state == "todo":
                state = "waiting:" + ",".join(pending) if pending else "ready"
            print(f"{task['id']:3}  {state:24}  {task['title']}")
        print("This command reports task state and starts no agents.")
    else:
        task = by_id.get(args.task_id)
        if task is None:
            parser.error("Specify an existing task, e.g. prompt T02")
        pending = [d for d in task["depends_on"] if by_id[d]["status"] != "done"]
        if pending or task["status"] in {"blocked", "done"}:
            print("Task unavailable: dependencies unfinished, task blocked or already done.", file=sys.stderr)
            return 1
        print(f"Выполни только {task['id']}: {task['title']}.")
        print(f"Файл задания: {task['file']}; ссылки brief относительны этому файлу.")
        print("Прочитай AGENTS.md и входные документы задачи. Сначала проверь актуальное поручение пользователя.")
        print("Этот brief сам не разрешает implementation, live-доступ, запуск других агентов или deploy.")
        print("Зафиксируй owner/status через координатора, затем работай в границах задания.")
        print("Требования: " + ", ".join(task["requirements"]))
        print("Сценарии: " + ", ".join(task["scenarios"]))
        print("\n" + (ROOT / task["file"]).read_text())
        print(f"Отчёт: tasks/reports/{task['id']}.md; результат и evidence передай координатору.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
