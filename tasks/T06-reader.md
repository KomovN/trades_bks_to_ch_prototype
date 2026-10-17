# T06 — WebSocket reader и reconnect

## Цель

Получение сделок, своевременный refresh долгой сессии и повторная подписка без retry storm.

## Входные документы

- [Протокол](../docs/specs/bks-protocol.md), [надёжность](../docs/specs/reliability.md).
- [Архитектура](../docs/architecture.md), [приёмка](../docs/acceptance.md).
- Контракты parser/token_provider из отчётов T03/T04.

## Файлы

`app/ws_reader.py`, `tests/test_ws_reader.py`, отчёт `tasks/reports/T06.md`.
Не менять полную подписку и не открывать лишние соединения по предположению о лимитах.

## Требования

R01, R04, R07, R08, R11. Зафиксировать received_at сразу после recv. Подписывать
101 инструмент при каждом connect; зафиксировать max_size/max_queue WS библиотеки.
Backoff+jitter с reset только после устойчивой сессии. Полученный TTL провоцирует
reconnect до expiry даже при активном потоке. queue.put прерывается при stop.

## Приёмка

- S10/S11/S14 проходят с fake WS/auth clock, без реальной сети.
- Handshake 401/403, structured auth error, close и static token отказ различаются.
- NoData не вызывает storm; неправильная подписка даёт fatal subscription error.
- Нет поиска auth-подстрок внутри raw сделки и логирования full payload.
- Producer завершение явно сигнализируется T07, гонка put/stop не теряет учёт строки.

## Проверки

`python -m pytest tests/test_ws_reader.py`; `make check`.
Live БКС оставляется S17, fake transport не доказывает реальный wire format.
