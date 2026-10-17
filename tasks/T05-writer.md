# T05 — ClickHouse batch writer

## Цель

Один writer без блокировки event loop, с ограниченными batch и корректной
семантикой подтверждения очереди.

## Входные документы

- [Данные](../docs/specs/data-contract.md), [надёжность](../docs/specs/reliability.md).
- [Архитектура](../docs/architecture.md), [конфигурация](../docs/specs/configuration.md).
- [Приёмка](../docs/acceptance.md).

## Файлы

`app/ch_writer.py`, `tests/test_ch_writer.py`, отчёт `tasks/reports/T05.md`.
Не выполнять DDL в ingest и не создавать дисковый буфер/дедупликацию.

## Требования

R05, R06, R13. INSERT с явным column order. Flush по размеру/времени первой строки,
retry сохраняет тот же batch. task_done после успешного INSERT. Временные ошибки
и permanent schema/access failures различаются. Client закрывается/сбрасывается безопасно.
Ограниченные driver timeouts совместимы с заданием T07 на shutdown.

## Приёмка

- S06/S07/S08 проходят с fake client/clock, без внешнего CH.
- Существует одновременно не более одного INSERT; поток I/O не оставляется бесконтрольно.
- Нет пустых запросов и busy loop, очередь не подтверждается до ack.
- При uncertain commit повтор идентичен, возможные дубли явно отражены в тесте/отчёте.
- Writer учитывает producer_done отдельно от stop, работает с общим drain deadline;
  точный lifecycle API согласован для интеграции T07.

## Проверки

`python -m pytest tests/test_ch_writer.py`; `make check`.
Фактический SQL/codec roundtrip проверяется в T08, fake client не доказывает CH-совместимость.
