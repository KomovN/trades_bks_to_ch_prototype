# T01 — Контракт и проверяемые fixtures

## Цель

Проверить, что выбранный протокол однозначен для реализации, а синтетические
примеры различают payload, ack и ошибку. Повторное полное исследование проекта не нужно.

## Входные документы

- [Протокол](../docs/specs/bks-protocol.md), [данные](../docs/specs/data-contract.md).
- [Решения](../docs/decisions.md), [источники](../docs/references.md).
- [Fixtures](../docs/fixtures/README.md), [приёмка](../docs/acceptance.md).

## Файлы

`docs/specs/bks-protocol.md`, `docs/fixtures/messages.json`, `docs/fixtures/README.md`,
`docs/open-questions.md`, отчёт `tasks/reports/T01.md`.
Не менять согласованный список 101 инструмента, DDL, соседние репозитории и runtime.

## Требования

R01, R04. Подтверждение пользователя о subscribeType/LastTrades уже записано.
Нужно проверить precedence ошибок, ack с метаданными и trade-shaped payload с
неизвестными полями; добавить примеры пограничных случаев по необходимости.
Если sparse payload остаётся неопределённым, явно оставить Q07 до live-проверки.

## Приёмка

- Таблица решений даёт один kind на каждый fixture без обращения к сети.
- Source error не превращается в trade; LastTrades trade не отбрасывается из-за PDF.
- LastTradesSuccess payload помечен как synthetic; источник списка — quotes.
- Оставшиеся Q02/Q03/Q07 перечислены без ложного утверждения о live-проверке.
- Изменения переданы T03/T06 и записаны в evidence.

## Проверки

`make check`; ручная трассировка каждого fixture по правилам протокола.
Проверки S02/S05/S11 затем реализуются в T02/T03/T06. Live не требуется для
завершения этой offline-задачи и не может считаться выполненным здесь.
