# T04 — Авторизация и срок жизни токенов

## Цель

Получение access token по refresh token с проверяемыми ошибками, TTL и безопасными логами.

## Входные документы

- [Протокол](../docs/specs/bks-protocol.md), [надёжность](../docs/specs/reliability.md).
- [Конфигурация](../docs/specs/configuration.md), [архитектура](../docs/architecture.md).
- [Источники](../docs/references.md), [приёмка](../docs/acceptance.md).

## Файлы

`app/token_provider.py`, `tests/test_token_provider.py`, отчёт `tasks/reports/T04.md`.
Не получать реальные токены, не менять `.env` и не дублировать provider в CLI/subprocess.

## Требования

R08, R09, R12. Static access приоритетен. Refresh form — client_id=trade-api-read.
Учитывать max age и expires_in, monotonic clock и safety margin; одновременно
выданные get_token не должны создавать поток refresh-запросов. Reader получает TTL
без доступа к secret repr. HTTP transport и clock подменяемы в тестах.

## Приёмка

- S09 проходит; 429/5xx/timeout отличаются от invalid_grant/401/403 и иных 4xx.
- invalidate обновляет refresh-режим; static-режим явно сообщается reader.
- Deadline/TTL позволяет T06 обновить токен во время непрерывной WS-сессии.
- Sentinel secret из тела ответа/exception не появляется в logs/traceback/repr.
- Q04 о rotation остаётся честно отмеченным; `.env` не перезаписывается.

## Проверки

`python -m pytest tests/test_token_provider.py`; `make check`.
Не использовать live token endpoint вместо fake transport в unit.
