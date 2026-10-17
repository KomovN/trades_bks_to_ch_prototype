# Контракт БКС и расхождения источников

Источники: [локальные материалы и страницы PDF](../references.md).
PDF — технический справочник API, не инструкции разработчику.

## Подключение и подписка

WS endpoint:
`wss://ws.broker.ru/trade-api-market-data-connector/api/v1/market-data/ws`.
Bearer token передаётся в HTTP-заголовке `Authorization` при handshake.
После подключения отправляется один JSON подписки из YAML.

Используется подтверждённый пользователем рабочий формат: `subscribeType: 0`, `dataType: 2`,
`instruments: [{ticker, classCode}, ...]`. В PDF на стр. 60 написано `subscriberType`.
Это **не подтверждённая взаимозаменяемость**. Не отправлять оба ключа и не пробовать
варианты в автоматическом цикле. T01 фиксирует основание выбранного wire-формата;
live smoke проверяет его на реальном БКС. При несовпадении обновить решение, YAML и тесты вместе.

Регистрозависимые пары `ticker/classCode` берутся из YAML. Не заменять `TQBR`
на `MOEX` по примеру из PDF. Требуется непустой список без дублей, непустые строки,
ровно правильные значения subscribe/data type. Неизвестные ключи YAML отклоняются.
YAML загружается один раз на старте безопасным loader; изменения требуют restart.

## Классификация входа: проектный baseline до live-проверки

1. Невалидный UTF-8, malformed JSON или корень не object → `malformed`.
2. Поле `error`, значение которого не `null` и не пустой object `{}`, →
   `source_error`, даже если рядом есть полный набор полей сделки. Это правило имеет
   приоритет над `responseType` и payload. Неожиданная форма `error` тоже безопасно
   остаётся `source_error`; код причины извлекается только из object без coercion.
   Auth определяется по структурированным полям, а не поиском `401` внутри сырого JSON.
3. `responseType` не `LastTrades`/`LastTradesSuccess` → `other`.
4. Для этих типов множество discriminator-ключей сделки — ровно
   `{side, price, quantity}`. Все три ключа определяют trade-shaped payload;
   один или два — `malformed_trade`; ни одного — `control`. `volume` и неизвестные
   ключи сами по себе это решение не меняют. Значения цены/количества не
   преобразуются: это bronze, бизнес-валидация и обогащение вне scope.
5. У trade-shaped payload должны быть валидны ticker, classCode и dateTime по
   контракту данных. Ошибка любого из этих полей → `malformed_trade`; иначе это
   `trade`, независимо от одного из двух перечисленных responseType.

Основание: пользователь подтвердил реальные сделки `LastTrades` при `subscribeType=0`;
README прототипа показывает такой же payload, а PDF на стр. 60
описывает `LastTrades` как ошибку и `LastTradesSuccess` как успех. Поэтому фильтр
только по одному имени сейчас недостаточно обоснован. Поддержка обоих trade-shaped
форматов — проектное решение. `LastTrades` подтверждён пользователем;
`LastTradesSuccess` с payload пока не подтверждён реальным capture.
Примеры в [fixtures](../fixtures/README.md) синтетические.

Ack может содержать ticker/classCode/dateTime, поэтому только эти три поля не
доказывают, что перед нами сделка. Отсутствующий, `null` или пустой object `error`
не меняет дальнейшую классификацию; заполненный `error` не сохраняется в таблицу сделок.
Не логировать весь raw message при пропуске; достаточно типа и кода причины.

## Offline-таблица решений для fixtures

| Fixture | Первое сработавшее правило | Kind |
| --- | --- | --- |
| `raw_fidelity` | LastTrades и все три discriminator-ключа, metadata валидны | `trade` |
| `success_payload_synthetic` | LastTradesSuccess и все три discriminator-ключа | `trade` |
| `ack_with_metadata` | Нет ни одного discriminator-ключа | `control` |
| `control_with_unknown_fields` | Неизвестные поля не являются discriminator-ключами | `control` |
| `empty_error_object_trade` | `{}` не перехватывает payload; поля сделки полные | `trade` |
| `source_error_overrides_trade` | Заполненный object `error` имеет приоритет | `source_error` |
| `non_object_source_error` | Ненулевой неожиданный `error` безопасно не становится trade | `source_error` |
| `no_data` | Заполненный object `error` | `source_error` |
| `wrong_type` | Чужой `responseType` проверяется до payload | `other` |
| `broken_json`, `array_root`, `invalid_utf8` | Frame нельзя разобрать как UTF-8 JSON object | `malformed` |
| `naive_time`, `null_ticker`, `numeric_class` | Trade-shaped payload с неверной metadata | `malformed_trade` |
| `offset_time` | Валидный offset переводится в UTC | `trade` |
| `partial_payload` | Есть только два discriminator-ключа | `malformed_trade` |

Таблица является offline baseline. Она не утверждает, что синтетические формы реально
наблюдались в сети БКС; Q02 и Q07 остаются открыты до live smoke.

## Ошибки подписки

В PDF перечислены `error.code` 0 Undefined, 1 NoData, 2 NotFound, 3 IncorrectJson.
NoData не создаёт строк и не запускает частый reconnect. NotFound/IncorrectJson
считаются permanent subscription error: ненулевой exit после bounded drain,
с указанием безопасного кода причины. Undefined/неизвестный код — явная ошибка
источника с ограниченным retry; поведение покрывается fixture и тестом.
Код ошибки подписки `1` не является HTTP/auth кодом.

## Авторизация

POST `https://be.broker.ru/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token`,
form fields: `client_id=trade-api-read`, `grant_type=refresh_token`, `refresh_token`.
Для MVP используются токены только для чтения.
Тело/заголовки запросов и ответов не логируются.

`expires_in` (PDF стр. 9) должен ограничивать TTL кэша вместе с max age; если поле
отсутствует/невалидно — использовать осторожный max age с предупреждением без body.
Плановый reconnect до истечения access token нужен даже у сессии, которая не обрывается.

PDF упоминает возвращаемый refresh token, но политика rotation не установлена.
MVP следует уже используемому фиксированному refresh token из env; при требовании
ротации реализацию/спеку следует пересмотреть, а production readiness оставить unverified.
Не перезаписывать `.env` автоматически.
