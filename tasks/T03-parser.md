# T03 — Parser и bronze row

## Цель

Чистый разбор frame с точным сохранением raw JSON и timestamp приёма.

## Входные документы

- [Архитектура](../docs/architecture.md), [данные](../docs/specs/data-contract.md).
- [Протокол](../docs/specs/bks-protocol.md), [fixtures](../docs/fixtures/messages.json).
- [Приёмка](../docs/acceptance.md).

## Файлы

`app/parser.py`, `tests/test_parser.py`, отчёт `tasks/reports/T03.md`.
Не менять DDL и не использовать Pydantic-сериализацию входной сделки.

## Требования

R02, R03, R04. Реализовать `TradeRawRow`, `ParseResult`, `parse_message` из
архитектуры. received_at приходит от reader; parser не берёт now самостоятельно.
UTC offset обязателен, индексируемое время — миллисекунды; raw хранит исходную точность.
Строгие строки ticker/classCode без coercion. Значения сделки не обогащаются.

## Приёмка

- S03/S04/S05 проходят на согласованных fixtures и дополнительных негативных входах.
- Неизвестные поля/пробелы/escape/формат цены сохраняются посимвольно.
- Неверный UTF-8 отклоняется без `errors=ignore/replace`.
- Error с полноценными полями сделки не становится row; ack также не становится row.
- Во входном `received_at` нет сдвига из-за ожидания queue/INSERT.

## Проверки

`python -m pytest tests/test_parser.py`; `make check`.
В отчёте указать публичный интерфейс и пример row для T05/T06.
