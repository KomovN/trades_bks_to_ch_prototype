# T02 — Python-каркас, settings и подписка

## Цель

Сделать воспроизводимую установку Python 3.12, безопасные настройки и загрузку
полной подписки. Сетевой runtime пока не нужен.

## Входные документы

- [Сервис](../docs/specs/service.md), [конфигурация](../docs/specs/configuration.md).
- [Архитектура](../docs/architecture.md), [протокол](../docs/specs/bks-protocol.md).
- [Приёмка](../docs/acceptance.md), [.env.example](../.env.example).

## Файлы

`pyproject.toml`, выбранный lock/constraints, `app/__init__.py`, `app/settings.py`,
`app/subscribe.py`, `tests/test_settings.py`, `tests/test_subscribe.py`.
Изменения общих `Makefile`, CI и README интегрировать через координатора.
Не создавать fake main, который выглядит готовым ingest-сервисом.

## Требования

R01, R12. Установка не требует БКС/CH. Фиксировать совместимые зависимости,
подтверждённые реальными проверками; не копировать версии наугад из прототипа.
Настройки соответствуют env table, проверяют finite floats/strict bools и скрывают
секреты. YAML safe loader, неизвестные поля/дубли/неверные types отклоняются.
Подписка точно сохраняет все 101 пары из согласованного snapshot и регистр.

## Приёмка

- Чистая установка в venv и импорт пакета под Python 3.12 успешны.
- Выполнены S01/S02; валидация происходит до сети.
- Настроены pytest markers `integration`, `live`; обычные unit не ходят в сеть.
- CI получает отдельный unit job; Harness job сохраняется и не заменяет unit.
- Нет runtime-импорта из quotes/prototype и нет секретов в repr/traceback.

## Проверки

`python -m pip install -e '.[dev]'` в выделенном venv;
`python -m pytest tests/test_settings.py tests/test_subscribe.py`;
`make check`. Записать проверенные версии и команду воспроизводимой установки.
