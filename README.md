# trades-bks-to-ch

Сервис принимает обезличенные сделки БКС по WebSocket и записывает исходный JSON
в ClickHouse: WebSocket → ограниченная очередь в памяти → batch INSERT →
`trades_raw_json`.

Сервис использует Python 3.12/asyncio и полный список из 101 инструмента в
`subscribe_msg.yaml`. Очередь существует только в памяти: аварийный выход может
потерять принятые данные, а неопределённый результат INSERT может дать дубли.

## Локальные проверки

Unit/component/process suite не обращается к БКС:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -c constraints.txt -e '.[dev]'
PYTHON=.venv/bin/python make check unit
```

Реальный ClickHouse и container smoke запускаются в отдельном Compose project без
опубликованных host ports. Fake WebSocket принимает только тестовый Bearer и отдаёт
одну synthetic trade; токены БКС не нужны.

```bash
scripts/run_integration.sh
```

Скрипт проверяет pinned `clickhouse/clickhouse-server:24.8.4.13`, дважды применяет
DDL, сверяет схему/codecs/keys, exact `raw_json`, UTC milliseconds и запускает
готовый container image против fake WebSocket. При ошибке Docker или ClickHouse
команда завершается ненулевым кодом. Cleanup делает только `compose down` в project
`trades-bks-to-ch-integration`; test volume и данные не удаляются.

## Production Compose

Сервис предназначен для отдельного сервера с Docker Engine/Compose v2 и существующим
ClickHouse, доступным по IP/DNS и HTTP(S). Образ собирается на сервере под его
архитектуру. Python base: `python:3.12.13-slim-bookworm`, без digest и локальных путей.
Токен и ClickHouse credentials заполняются в `.env` только на сервере.

Полная инструкция: [`docs/operations.md`](docs/operations.md) — доставка архива,
настройка `.env`, HTTP/HTTPS, init с DDL-user, запуск, диагностика и rollback.
Краткий порядок после доставки исходников и перехода в их каталог:

```bash
cp .env.example .env
chmod 600 .env
chmod 644 subscribe_msg.yaml
${EDITOR:-vi} .env
TRADES_ENV_FILE=.env docker compose config --quiet
```

Для refresh-режима заполнить `REFRESH_TOKEN`, оставив `ACCESS_TOKEN` пустым.
`CLICKHOUSE_HOST` — IP/DNS без схемы и порта; порт и TLS задаются отдельно.
`docker-compose.yml` содержит только `trades-ingestor`.

Перед запуском узнайте production-версию ClickHouse и выдайте отдельному DDL-user
право создать выбранную database/table. Init выполняется отдельно от runtime:

```bash
TRADES_ENV_FILE=.env docker compose build --pull trades-ingestor
TRADES_ENV_FILE=.env docker compose run --rm --no-deps \
  -v "$PWD/migrations:/app/migrations:ro" \
  trades-ingestor python /app/scripts/init_clickhouse.py \
  /app/migrations/001_create_trades_raw_json.sql
TRADES_ENV_FILE=.env docker compose run --rm --no-deps \
  trades-ingestor python /app/scripts/init_clickhouse.py --check
TRADES_ENV_FILE=.env docker compose up -d --no-build trades-ingestor
```

Проверка состояния и штатная остановка:

```bash
TRADES_ENV_FILE=.env docker compose logs --tail=100 trades-ingestor
TRADES_ENV_FILE=.env docker compose stop -t 60 trades-ingestor
TRADES_ENV_FILE=.env docker compose down --remove-orphans
```

Последняя команда удаляет только Compose container/network этого проекта. Она не
удаляет таблицу, данные или volume существующего ClickHouse. Повторный init использует
`IF NOT EXISTS` и тоже не удаляет данные. Полный runbook находится в
[`docs/operations.md`](docs/operations.md), а согласованный DDL — в
[`migrations/001_create_trades_raw_json.sql`](migrations/001_create_trades_raw_json.sql).

## Документация

- [`AGENTS.md`](AGENTS.md) — рабочие соглашения.
- [`docs/index.md`](docs/index.md) — карта спецификаций.
- [`docs/acceptance.md`](docs/acceptance.md) — сценарии приёмки S01–S17.
- [`docs/open-questions.md`](docs/open-questions.md) — проверки до production.
- [`tasks/index.json`](tasks/index.json) — состояние задач Harness.

T01–T09 и offline-приёмка завершены, отчёты — в [`tasks/reports/`](tasks/reports/).
Live БКС/production CH здесь не запускались: S17 остаётся `not run` до проверки
на сервере. Повторная проверка и подготовка передачи описаны в
[`docs/reviews/remote-deployment.md`](docs/reviews/remote-deployment.md).
