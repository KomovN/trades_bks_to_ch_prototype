# Конфигурация

Шаблон: [.env.example](../../.env.example). Runtime читает env; `.env` удобен локально,
не коммитится. Секреты не входят в repr настроек, diagnostics и tracebacks.

| Переменная | Default | Ограничение |
| --- | --- | --- |
| WS_URL | market-data endpoint из примера | production `wss`; `ws` только loopback для теста |
| SOURCE | bks | непустая string |
| SUBSCRIBE_CONFIG | subscribe_msg.yaml | файл существует, YAML валиден |
| ACCESS_TOKEN | пусто | приоритет над refresh; значение секретно |
| REFRESH_TOKEN | пусто | нужен если access отсутствует; значение секретно |
| ACCESS_TOKEN_MAX_AGE_SEC | 82800 | > 0; фактический TTL не больше `expires_in` minus safety margin |
| AUTH_FAILED_EXIT_DELAY_SEC | 600 | >= 0; прерывается сигналом |
| CLICKHOUSE_HOST | 127.0.0.1 | непустой host без URL/credentials |
| CLICKHOUSE_PORT | 8123 | 1…65535 |
| CLICKHOUSE_USER | default | непустая string |
| CLICKHOUSE_PASSWORD | пусто | секрет |
| CLICKHOUSE_DATABASE | bronze | простой identifier `[A-Za-z_][A-Za-z0-9_]*`; для канонического DDL значение bronze |
| CLICKHOUSE_TABLE | trades_raw_json | в MVP только это значение; DDL имя фиксировано |
| CLICKHOUSE_SECURE | false | строгий bool; неизвестные значения отклоняются |
| CLICKHOUSE_CONNECT_TIMEOUT_SEC | 5 | > 0 и <= 10; вместе с I/O timeout <= 15 |
| CLICKHOUSE_IO_TIMEOUT_SEC | 10 | > 0 и <= 10; вместе с connect timeout <= 15 |
| BATCH_SIZE | 1000 | integer >= 1 |
| FLUSH_INTERVAL_SEC | 1.0 | finite float > 0 |
| QUEUE_MAXSIZE | 10000 | integer >= 1 |
| RECONNECT_MIN_DELAY_SEC | 1 | finite > 0 |
| RECONNECT_MAX_DELAY_SEC | 60 | finite >= min |
| SHUTDOWN_TIMEOUT_SEC | 30 | finite > 0, > CH I/O timeout |
| LOG_LEVEL | INFO | допустимый logging level |
| LOG_SUMMARY_INTERVAL_SEC | 60 | finite > 0 |

Статические значения batch/queue — стартовые defaults, не измеренная производительность.
Число элементов ограничено; память также зависит от размера frame. В T06 задаётся
явный WS max frame size (baseline 1 MiB) и ограничиваются внутренние буферы клиента.
Превышение лимита не маскируется под нормальный idle.

Канонический DDL явно создаёт `bronze.trades_raw_json`. Init сверяет его database
с `CLICKHOUSE_DATABASE` до сети и DDL, не переписывает SQL под окружение. Runtime
использует database из env и неквалифицированное `CLICKHOUSE_TABLE=trades_raw_json`;
для этой миграции в существующем серверном env также нужно выбрать `bronze`.
Сервис не создаёт database/table автоматически.
Список инструментов находится только в [subscribe_msg.yaml](../../subscribe_msg.yaml).
