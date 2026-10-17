# T07 — Supervisor, сигналы и наблюдаемость

## Цель

Собрать pipeline, завершить процесс за ограниченное время и сделать состояние ingest наблюдаемым.

## Входные документы

- [Надёжность](../docs/specs/reliability.md), [архитектура](../docs/architecture.md).
- [Конфигурация](../docs/specs/configuration.md), [приёмка](../docs/acceptance.md).
- Отчёты T05/T06 с фактическими API.

## Файлы

`app/main.py`, `app/logging_setup.py`, `tests/test_lifecycle.py`, `tests/test_logging.py`,
при необходимости интеграционные правки `app/ch_writer.py`/`app/ws_reader.py` и их тестов.
Общие Makefile/CI интегрировать через координатора. Не добавлять HTTP monitoring server.

## Требования

R09, R10, R15. Supervisor отслеживает reader и writer. Один monotonic deadline
устанавливается при запросе остановки. Сначала producer закрывается, потом drain.
Выход 0 только если принятые строки дописаны; fatal/deadline → 1. Auth delay
прерывается сигналом. Summary без логирования каждой сделки и без секретов.

## Приёмка

- S12/S13/S16 проходят, включая настоящий subprocess для SIGTERM/SIGINT.
- Проверены recv, full queue, CH retry, worker crash, producer/consumer race.
- По defaults недоступный CH не удерживает процесс дольше 45 сек;
  bounded I/O thread завершён, просто cancel coroutine недостаточно.
- Счётчики accepted/inserted/unsaved согласованы при остановке.
- Логи исключений библиотек не раскрывают sentinel secrets; idle summary не спамит.

## Проверки

`python -m pytest tests/test_lifecycle.py tests/test_logging.py`; `make unit`; `make check`.
Время процессного теста измерять; искусственно ускоренный fake clock — дополнение,
а не замена проверки фактического завершения Python process.
