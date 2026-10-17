# T08 — Docker, локальный ClickHouse и runbook

## Цель

Воспроизводимый контейнер и integration без БКС, без конфликтов с существующим quotes/CH.

## Входные документы

- [Эксплуатация](../docs/operations.md), [приёмка](../docs/acceptance.md).
- [Данные](../docs/specs/data-contract.md), [конфигурация](../docs/specs/configuration.md).
- [Открытые вопросы](../docs/open-questions.md).

## Файлы

`Dockerfile`, `.dockerignore`, `docker-compose.yml`, `docker-compose.local.yml`,
integration Compose/fixtures, `tests/integration/`, README/runbook.
CI и Makefile согласовать через координатора. Пользовательский SQL не менять.
Не подключаться к production и не применять внешние миграции в рамках этой задачи.

## Требования

R13, R14, R16. Production Compose содержит только trades-ingestor, local/integration
CH изолирован, tag зафиксирован после проверки, без latest. Секреты и соседние файлы
не попадают в image context. Grace period 60s, read-only YAML, ограниченные Docker logs.
Отдельный init шаг выбирает database. Не удалять данные при повторном init/cleanup.

## Приёмка

- S15 проходит на настоящем локальном CH, DDL дважды применим в тестовой database.
- Проверены codecs, key/partition/order, exact raw SELECT и UTC milliseconds.
- Container smoke использует fake WS/auth, unit/integration не требует BKS секретов.
- Зафиксированы команды build/start/stop/init/query/cleanup; cleanup ограничен проектом.
- CI integration не делает silent skip при недоступном CH.
- Неизвестная production CH version остаётся Q01 до проверки перед deployment.

## Проверки

`docker compose config` с временным безопасным env;
`docker compose -f docker-compose.local.yml config`; build/smoke в тестовом проекте;
`make unit`; `make integration`; `make check`.
Сохранить проверенную версию image/driver и фактический результат SQL, без секретов.
