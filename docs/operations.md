# Запуск на удалённом сервере

Целевой вариант: Linux-сервер с Docker Engine и Docker Compose v2; существующий
ClickHouse доступен контейнеру по IP/DNS и HTTP(S)-порту. Python на сервере для
production-запуска не нужен: зависимости устанавливаются при сборке образа.

Runtime использует читаемый тег `python:3.12.13-slim-bookworm`. Образ собирается
на целевом сервере под его архитектуру; локальный Docker image с Mac переносить
не требуется. Production Compose запускает только `trades-ingestor` и не публикует
порты. Локальный ClickHouse используется только в integration Compose.

## 1. Доставить исходники

В репозитории подготовлен архив исходников из Git-коммита без `.env`, кешей и Docker images.
На машине, где лежит архив, подставить свои SSH user/host:

```bash
scp artifacts/trades-bks-to-ch-0.1.0.tar.gz USER@SERVER:~/
```

На сервере:

```bash
mkdir -p ~/services/trades-bks-to-ch
tar -xzf ~/trades-bks-to-ch-0.1.0.tar.gz -C ~/services/trades-bks-to-ch
cd ~/services/trades-bks-to-ch
docker --version
docker compose version
docker info >/dev/null
```

Если исходники уже доступны через Git, вместо архива можно клонировать репозиторий
и перейти в его каталог. Все дальнейшие команды выполняются из корня проекта.

## 2. Настроить окружение только на сервере

При первом запуске:

```bash
cp .env.example .env
chmod 600 .env
chmod 644 subscribe_msg.yaml
${EDITOR:-vi} .env
```

При обновлении существующий `.env` сохраняется; команду `cp` повторять не нужно.
Заполнить реальные значения на сервере, без отправки секретов в чат:

```dotenv
ACCESS_TOKEN=
REFRESH_TOKEN='SERVER_REFRESH_TOKEN'
CLICKHOUSE_HOST=ch.example.internal
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=trades_writer
CLICKHOUSE_PASSWORD='SERVER_CH_PASSWORD'
CLICKHOUSE_DATABASE=market_data
CLICKHOUSE_TABLE=trades_raw_json
CLICKHOUSE_SECURE=false
```

Это пример формы, а не готовые credentials. Для HTTPS указать реальный HTTPS-порт
сервера и `CLICKHOUSE_SECURE=true`. В `CLICKHOUSE_HOST` нужен только IP/DNS, без
`http://`, `https://`, порта или пути. Использовать фактический HTTP(S)-порт своего
ClickHouse; native TCP-порт 9000 для этого драйвера не подходит.

Для автоматического обновления токена заполнить `REFRESH_TOKEN` и оставить
`ACCESS_TOKEN` пустым. Если указан `ACCESS_TOKEN`, он имеет приоритет и используется
как статический: автоматически через refresh он не обновляется. `WS_URL` в шаблоне
уже содержит endpoint БКС. Остальные defaults подходят для первого запуска;
`subscribe_msg.yaml` содержит все 101 согласованные пары из quotes.

При паролях со специальными символами использовать одинарные кавычки по синтаксису
Compose env-файла. `.env` читает Compose на сервере, а процесс получает переменные
окружения; файл не монтируется внутрь image. Настройки batch/queue — стартовые
значения, а не гарантия пропускной способности.

## 3. Собрать image и проверить конфигурацию

```bash
TRADES_ENV_FILE=.env docker compose config --quiet
TRADES_ENV_FILE=.env docker compose build --pull trades-ingestor
```

`config --quiet` проверяет Compose без печати значений env. Сборка обращается к
registry/PyPI; сама команда build не запускает БКС или ClickHouse-клиент. В Dockerfile
не задана конкретная CPU-архитектура или путь машины разработчика.

## 4. Проверить ClickHouse и применить DDL отдельно

До init администратор через существующий SQL-клиент проверяет:

```sql
SELECT version(), timezone();
```

Если таблица уже существует, также выполнить в выбранной database:

```sql
SHOW CREATE TABLE trades_raw_json;
```

Сверить схему с [`migrations/001_create_trades_raw_json.sql`](../migrations/001_create_trades_raw_json.sql).
`IF NOT EXISTS` не исправляет уже существующую несовместимую схему. Offline-проверка
прошла на ClickHouse `24.8.4.13`; версия внешнего сервера пока не проверялась.

Init создаёт выбранную database при отсутствии и применяет канонический DDL.
Он требует DDL-права; ingestion сам миграции не запускает.

Если пользователь из `.env` имеет нужные DDL-права:

```bash
TRADES_ENV_FILE=.env docker compose run --rm --no-deps \
  -v "$PWD/migrations:/app/migrations:ro" \
  trades-ingestor python /app/scripts/init_clickhouse.py \
  /app/migrations/001_create_trades_raw_json.sql
```

Если DDL выполняется отдельным пользователем, создать на сервере `.env.init` на основе
`.env`, заменить в нём только ClickHouse credentials на административные, поставить
`chmod 600 .env.init` и выполнить ту же команду с `TRADES_ENV_FILE=.env.init`.
Дальнейший запуск использует `.env` с runtime-user. Оба env-файла исключены из Git
и Docker build context. Init не открывает WebSocket БКС и не запрашивает BKS token.

Проверить подключение из app-контейнера, версию, timezone, схему и количество строк:

```bash
TRADES_ENV_FILE=.env docker compose run --rm --no-deps \
  trades-ingestor python /app/scripts/init_clickhouse.py --check
```

Для этой диагностической команды нужны SELECT/доступ к metadata в дополнение к INSERT.
Обычный ingestion записывает данные без DDL-доступа. Если создание database/table уже
выполнил администратор, init пропускается; остаётся проверка существующей схемы.

## 5. Запустить и наблюдать

Именно следующая команда запускает приложение, авторизацию БКС и WebSocket-подписку:

```bash
TRADES_ENV_FILE=.env docker compose up -d --no-build trades-ingestor
TRADES_ENV_FILE=.env docker compose ps
TRADES_ENV_FILE=.env docker compose logs --tail=100 -f trades-ingestor
```

`Ctrl+C` останавливает просмотр логов, контейнер продолжает работать. Summary по
умолчанию появляется раз в 60 секунд. В торговое время должны расти `accepted_trades`
и `inserted_rows`; `unsaved_rows=0` означает отсутствие известных недописанных строк.
Вне торгов отсутствие строк допустимо. Статус `running` сам по себе не доказывает
успешную подписку или приём всех инструментов: это проверяется live smoke на сервере.

Повторный `--check` показывает `rows` и `max_received_at`. `count()` считает физические
строки; дубли не удаляются. При auth rejection сервис делает drain, ждёт по умолчанию
600 секунд, затем выходит с code 1; сигнал прерывает задержку. Обновление `.env`
требует пересоздания контейнера:

```bash
TRADES_ENV_FILE=.env docker compose up -d --no-build --force-recreate trades-ingestor
```

## Остановка, обновление и rollback

```bash
TRADES_ENV_FILE=.env docker compose stop -t 60 trades-ingestor
```

В финальном summary проверить `unsaved_rows`. Очередь в памяти теряется при аварии;
retry неопределённого INSERT может породить дубли. TTL отсутствует по принятому DDL.
Поток сделок за время остановки автоматически не восстанавливается.

Перед обновлением сохранить предыдущий image и остановить приложение:

```bash
docker image tag trades-bks-to-ch:0.1.0 trades-bks-to-ch:previous
TRADES_ENV_FILE=.env docker compose stop -t 60 trades-ingestor
```

Доставить новые исходники, сохранив серверный `.env`, затем:

```bash
TRADES_ENV_FILE=.env docker compose build --pull trades-ingestor
TRADES_ENV_FILE=.env docker compose up -d --no-build trades-ingestor
```

Rollback:

```bash
TRADES_IMAGE=trades-bks-to-ch:previous TRADES_ENV_FILE=.env \
  docker compose up -d --no-build trades-ingestor
```

Для удаления только app-контейнера и его сети:

```bash
TRADES_ENV_FILE=.env docker compose down --remove-orphans
```

Команды не удаляют внешнюю таблицу или данные ClickHouse. Не менять имя production
Compose project на имя quotes-проекта. Root filesystem app read-only, процесс работает
под uid/gid 10001; subscription YAML доступен на чтение. Docker logs ограничены тремя
файлами по 10 MiB, grace period равен 60 секундам.

## Проверки без production credentials

На машине с Python 3.12: `PYTHON=.venv/bin/python make verify` после установки
`pip install -c constraints.txt -e '.[dev]'`. Альтернатива на сервере только с Docker:

```bash
docker build --target tests -t trades-bks-to-ch-tests:0.1.0 .
docker run --rm --network none trades-bks-to-ch-tests:0.1.0 \
  python -m pytest tests -m 'not integration and not live'
scripts/run_integration.sh
```

Offline suite использует fake transports; процессный CH-down тест обращается только
к `127.0.0.1:9`. Integration поднимает собственный CH `24.8.4.13` без host ports,
использует synthetic credentials и локальный fake WS. Тесты не используют серверный
`.env`. Имя test project фиксировано: `trades-bks-to-ch-integration`, независимо от
внешнего `COMPOSE_PROJECT_NAME`. Cleanup удаляет только его containers/network и
сохраняет test volume; ошибка тестов или cleanup даёт nonzero.

## Состояние готовности

T01–T09 завершены; offline MVP проверен. Live S17, совместимость с внешним ClickHouse,
поведение refresh token при совместном использовании с quotes и короткий soak
остаются серверными проверками. Список вопросов — [`open-questions.md`](open-questions.md).
Секреты и адрес production CH на машине разработчика не задавались; SSH/deploy здесь
не выполнялись.
