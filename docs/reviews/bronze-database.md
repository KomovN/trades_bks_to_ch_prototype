# Проверка database bronze

Дата: 2026-10-17. Основание — исправление пользователя в каноническом DDL:
`CREATE TABLE IF NOT EXISTS bronze.trades_raw_json`.

## Найденное несоответствие

После изменения только SQL приложение и init по умолчанию выбирали `default`, а
изолированный integration — `trades_integration`. Qualified имя в SQL не меняет
database клиента. Поэтому init мог не найти `bronze` либо создать таблицу там,
где runtime её не ищет. Harness также отклонил DDL по прежнему baseline hash.

## Исправления

- Пользовательский DDL сохранён; колонки, engine, ключи, codecs и partition не менялись.
- Runtime/init default, `.env.example`, safe env и local Compose согласованы на `bronze`.
  INSERT и диагностика используют `trades_raw_json` внутри database клиента `bronze`.
- Init сверяет database из `CLICKHOUSE_DATABASE` с квалифицированным target DDL
  до открытия клиента и CREATE DATABASE; SQL не переписывается. Возможность явно
  выбирать runtime database через env сохранена, но для канонической миграции
  требуется `bronze`.
- Обновлены schema baseline, решение D16, configuration/data-contract и runbook.
- Добавлены offline-проверки default/explicit bronze, несовпадения database и
  неожиданного target; integration проверяет фактическую database и отсутствие
  создания посторонней database при ошибочном env.

## Проверки

- Адресный settings/init suite — exit 0, `36 passed`.
- `PYTHON=<CPython 3.12.13> make verify` — exit 0: Harness PASS,
  `173 passed, 4 deselected in 33.89s`; init сообщил `database=bronze`, server
  `24.8.4.13`; schema/check/mismatched-database suite — `3 passed in 0.78s`,
  container entrypoint smoke — `1 passed in 0.61s`.
- `docker run --rm --network none trades-bks-to-ch-integration-integration
  python -m pytest tests -m 'not integration and not live' -q` — exit 0,
  `173 passed, 4 deselected in 32.73s` под Linux без внешней сети.
- `compileall`, оба Compose config с synthetic env и `git diff --check` — exit 0.
- Первый Docker build прервался на timeout PyPI; повтор с тем же Dockerfile прошёл.
  Docker tags, timeout и зависимости ради этой сетевой ошибки не менялись.
- Отдельно сравнено тело DDL с предыдущим Git-коммитом: изменён только пользовательский
  квалификатор `bronze.`; все определения колонок/ключей сохранены.

Изолированный test project удалил свои containers/network, сохранив volume. Данные
из старой test database не удалялись. Production/live результаты не заявляются.

## На сервере

В существующем серверном `.env` нужны:

```dotenv
CLICKHOUSE_DATABASE=bronze
CLICKHOUSE_TABLE=trades_raw_json
```

Не добавлять `bronze.` в `CLICKHOUSE_TABLE`: database передаётся клиенту отдельно.
Серверный `.env` не перезаписывается шаблоном; старое явно заданное значение имеет
приоритет над default кода. После доставки новых исходников пересобрать image,
выполнить отдельный init при необходимости, `--check` и пересоздать ingestor.

Данные прежней database не перемещаются и не удаляются. Проверка использует только
изолированный локальный ClickHouse и fake WS; production CH/БКС не подключались.
