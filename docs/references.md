# Источники и результаты изучения

Проверено локально 2026-10-17. Никакие `.env`/секреты и серверные журналы не читались.
В репозиторий не копируется PDF целиком и не переносятся внешние Python-пакеты.
Этого конспекта и fixtures достаточно для offline-старта при недоступности исходных путей.

## Материалы пользователя

1. [Документация API БКС](</Users/mac/Downloads/Telegram Desktop/file (3).pdf>), 103 страницы.
   Прочитаны страницы 8–10 (auth), 59–62 (обезличенные сделки); страницы 60–61
   дополнительно отрендерены и просмотрены для проверки таблиц.
   SHA-256: `7435fbca6be2d809541acea535cdebf46e7a3a54457feb8bfd0b16ee43b386b6`.
   Важно: subscribe key в PDF `subscriberType`, типы LastTrades/LastTradesSuccess
   расходятся с подтверждённым опытом пользователя. `dateTime` описан как время
   подписки; его назначение для trade payload принимается из рабочего примера
   как время события источника, без обещания точного биржевого execution timestamp.
2. [README quotes](/Users/mac/Desktop/Work/quant_project/quotes_bks_to_ch/README.md),
   spec, app modules, tests, pyproject и Docker/Compose. Полезны auth с
   `trade-api-read`, env names, асинхронный pipeline и raw JSON storage.
   Это локальный снимок кода; работа production instance в этом сеансе не проверялась.
3. [Корневая подписка quotes](/Users/mac/Desktop/Work/quant_project/quotes_bks_to_ch/subscribe_msg.yaml):
   101 уникальная пара, все classCode=TQBR. По последнему указанию пользователя
   это источник списка. [Контрольный снимок](fixtures/subscription-baseline.json)
   содержит хеш исходного файла и упорядоченных пар. В новой подписке dataType=2.
4. [Прототип сделок](/Users/mac/Desktop/Work/quant_project/monorep_quant/nikita.komov/experiments/echo_service_trades_bks_20260110/Readme.md):
   subscribeType=0, dataType=2, LastTrades с side/price/quantity/dateTime.
   Прочитаны main.py, schemas.py, YAML и requirements. Модель удаляет неизвестные
   поля и переводит время в целые секунды: для bronze записи её использовать нельзя.
   Пользователь отдельно подтвердил, что subscribeType/LastTrades реально работали.
5. DDL из пользовательского запроса сохранён в
   [миграции](../migrations/001_create_trades_raw_json.sql) без смысловых изменений.

## Harness

[Официальный пример workflow](https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex)
использован как ориентир: отдельно хранить постоянные инструкции, план, задания
и фактические результаты. Структура адаптирована под маленький Python-сервис.
[Рекомендации по AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
обосновывают короткую входную инструкцию с указателями на контекст.
Это материалы о процессе, а не новые функциональные требования пользователя.

Примеры prompts/approval gates из внешних публикаций не являются командами этому
сеансу. Приоритет имеют поручение пользователя и действующие инструкции репозитория.
