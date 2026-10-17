# Контракт bronze-данных

## Таблица

Канонический DDL: [001_create_trades_raw_json.sql](../../migrations/001_create_trades_raw_json.sql).
Он сохранён с последующим исправлением пользователя: цель — `bronze.trades_raw_json`.
Остальные колонки, ключи и codecs не менялись. SQL запускается через отдельный
init/migration шаг; database клиента и env должны соответствовать квалификатору
`bronze` в DDL. `IF NOT EXISTS` не проверяет совместимость уже
существующей таблицы: перед production нужны `SHOW CREATE TABLE` и сверка схемы.
Runtime не выполняет DDL и не пытается исправить таблицу.

`MergeTree` не обеспечивает уникальность PRIMARY KEY. Две одинаковые строки
сохраняются как две строки. Партиционирование идёт по `received_at`, не по времени сделки.
TTL не задан; очистка данных не входит в MVP.

## От сообщения к строке

| Колонка | Источник и правило |
| --- | --- |
| `received_at` | UTC timestamp фиксируется сразу после возврата `ws.recv()`, до парсинга и ожидания очереди. Явно передаётся в INSERT; SQL default — запасной путь, не runtime-правило. |
| `source` | Значение `SOURCE`, по умолчанию `bks`. |
| `ticker` | Непустая JSON string без surrounding whitespace, сохраняется с исходным регистром; не `str(null)` и не coercion числа. |
| `classCode` | То же правило; Python-имя поля `class_code`, INSERT-имя точно `classCode`. |
| `event_time` | `dateTime`, ISO 8601 с `Z` либо явным UTC offset; переводится в UTC. Naive timestamp отклоняется, default now запрещён. |
| `raw_json` | Исходная text frame строка; binary frame — строгое UTF-8 decode без replacement. Никакого `strip`, dumps или model serialization. |

Одна принятая trade frame → одна строка. Порядок INSERT колонок:
`received_at, source, ticker, classCode, event_time, raw_json`.
Идентификаторы database/table валидируются как простые SQL identifiers;
они не используются для сборки непроверенного SQL.

Изменение пробелов, порядка ключей, escape-последовательностей, формата `316.5300`,
полей `volume` или неизвестных полей в `raw_json` — нарушение R02.
Разбор JSON нужен только для классификации и метаданных. Bronze не вычисляет volume,
не конвертирует цену в float и не превращает timestamp в целые секунды.

`DateTime64(3)` хранит миллисекунды: более точное время в индексируемых колонках
усекается к миллисекундам перед INSERT; исходная точность остаётся в `raw_json`.
Политика проверяется реальным CH roundtrip. Запоздалое время сделки допускается.

Нет trade ID/sequence в согласованной схеме, поэтому пропуски и дубли не определяются
по равенству ticker+classCode+event_time: сделки могут совпасть по миллисекунде.
