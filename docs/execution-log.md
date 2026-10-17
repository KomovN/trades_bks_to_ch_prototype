# Журнал выполнения

## 2026-10-17: подготовка Harness

Пользователь поручил подготовку репозитория и заданий без реализации сервиса.
Изучены API PDF, локальный quotes-сервис и прототип сделок. Зафиксированы ответы
о стеке, очереди в памяти, DDL и рабочем wire-формате. Позднейшая поправка списка
заменила 16 бумаг прототипа на полный список quotes: 101 пара.

Runtime-задачи T01–T09 ещё не выполнялись. Агенты не запускались. Миграция
к ClickHouse не применялась, сеть БКС/сервер не использовались.

Фактические проверки подготовки:

- `make check`: exit 0, 9 задач, R01–R16, S01–S17; ссылки, DAG, coverage,
  schema/subscription baselines и пустые секреты в env example проверены.
- `make tasks`: exit 0; T01/T02 готовы, остальные ожидают свои зависимости.
- `make prompt TASK=T01` и `make prompt TASK=T02`: exit 0, briefs генерируются.
- `make prompt TASK=T03`: ожидаемый отказ (скрипт exit 1, make exit 2),
  так как T01/T02 ещё не завершены.
- В копиях во временной директории все 8 негативных проверок отклонили:
  битую ссылку, цикл задач, отсутствие исполнителя требования, заполненный example
  secret, удалённую бумагу, изменённый DDL, done без evidence и пробел в acceptance coverage.
  Эти мутации не применялись к рабочему репозиторию.
- Прямое сравнение с исходным quotes YAML: все 101 пары и их порядок совпадают,
  меняется только dataType 3 → 2 и завершающий newline.
- `git diff --check`: exit 0. Так как репозиторий не имеет начального commit,
  отдельно проверены whitespace/final newline всех 39 новых файлов.

Harness проверен локально под Python 3.14.3 и целевым bundled Python 3.12.14.
Workflow GitHub Actions подготовлен, но на GitHub не запускался. Runtime unit,
process, Docker, CH integration и live БКС: **not run**, поскольку сервис не реализован.

## 2026-10-17: начало реализации

Пользователь разрешил запуск агентов и последовательное выполнение DAG с проверкой
каждого этапа. T01 назначена `/root/t01_protocol`, T02 — `/root/t02_foundation`;
обе задачи начаты параллельно. Общие реестр и журнал ведёт координатор.

### T01 — принято

Owner `/root/t01_protocol`. Контракт классификации уточнён и вручную трассирован
по 17 fixtures; добавлены edge cases для неизвестных полей, пустого error object
и non-object source error. Координатор повторно выполнил `make check` (exit 0)
и отдельную проверку 17 уникальных fixtures/6 kinds (exit 0). Evidence:
`tasks/reports/T01.md`. Live БКС не запускался; Q02, Q03 и Q07 остаются открыты.

### T02 — принято

Owner `/root/t02_foundation`. Добавлены installable Python 3.12 package, полный
constraints-граф, строгие settings, безопасный loader подписки и unit-тесты.
Агент в чистом CPython 3.12.13 установил пакет и получил 43 passed; координатор
повторил те же 43 теста (exit 0), `pip check` (exit 0), `compileall` (exit 0),
`make check` и `git diff --check` (exit 0). В CI добавлен отдельный offline unit job
с установкой через `constraints.txt`. Evidence: `tasks/reports/T02.md`.
Integration/live не запускались в scope T02.

После приёмки T01/T02 зависимости открыли T03 и T04. T03 назначена
`/root/t03_parser`, T04 — `/root/t04_auth`; задачи начаты параллельно.

### T03 — принято

Owner `/root/t03_parser`. Реализованы immutable bronze row/result и чистый parser
со строгим UTF-8/JSON, T01 precedence, raw fidelity и UTC millisecond timestamps.
Координатор повторно выполнил весь текущий offline suite под Python 3.12.13:
114 passed (включая уже появившиеся, но ещё не принятые тесты T04), `compileall`,
`make check` и `git diff --check` — exit 0. Evidence: `tasks/reports/T03.md`.
CH roundtrip и live frames остаются будущими проверками.

После приёмки T03 задача T05 назначена `/root/t05_writer` и начата параллельно
с T04. T06 остаётся закрыта до приёмки T04.

### T04 — принято

Owner `/root/t04_auth`. Реализованы static/refresh provider, monotonic TTL/deadline,
конкурентный refresh, invalidate и безопасная классификация auth ошибок. Координатор
повторил текущий offline suite под Python 3.12.13: 114 passed, `make check` и
`git diff --check` — exit 0. Evidence: `tasks/reports/T04.md`. Live endpoint не
вызывался; Q04 и HTTP-date Retry-After остаются явно записанными ограничениями.

После приёмки T04 задача T06 назначена `/root/t06_reader` и начата параллельно
с уже выполняющейся T05.

### T06 — принято

Owner `/root/t06_reader`. Реализованы bounded WS reader, полный subscribe/resubscribe,
planned token rotation, reconnect/backoff и producer completion contract. Координатор
повторил `tests/test_ws_reader.py`: 19 passed, и весь текущий offline suite:
152 passed под Python 3.12.13; `make check` и `git diff --check` — exit 0.
Evidence: `tasks/reports/T06.md`. Live БКС не использовался; фактические wire/auth
формы и лимит подписки остаются S17.

### T05 — принято

Owner `/root/t05_writer`. Реализована пакетная запись с явными INSERT columns,
таймером от первой строки, offload синхронного ClickHouse I/O, повтором неизменного
batch и подтверждением очереди только после успешного INSERT. Координатор повторил
`tests/test_ch_writer.py`: 19 passed, и весь offline suite: 152 passed под
Python 3.12.13; `make check` и `git diff --check` — exit 0. Evidence:
`tasks/reports/T05.md`. Совместимость с реальным ClickHouse проверит T08.

После приёмки T05 и T06 открыта T07 — lifecycle supervisor и bounded shutdown.

### T07 — принято

Owner `/root/t07_lifecycle`. Реализованы composition root, supervisor, POSIX signal
handlers, единый monotonic drain deadline, bounded shutdown и редактирование секретов
в логах. Координатор повторил lifecycle/logging suite: 10 passed за 30,84 с и весь
offline suite: 162 passed. Настоящий subprocess с SIGTERM и недоступным ClickHouse
завершился примерно за 30,3 с при границе 45 с; `make check` и `git diff --check` —
exit 0. Evidence: `tasks/reports/T07.md`.

После приёмки T07 открыта T08 — Docker, локальный ClickHouse и runbook.

### T08 — принято

Owner `/root/t08_deployment`. Добавлены multi-stage image, production Compose только
с ingestor, изолированный local Compose, отдельный init/check CLI, S15 и настоящий
container entrypoint smoke с fake WebSocket. Координатор с доступом к Docker daemon
проверил ClickHouse `24.8.4.13`: init успешен, schema/raw/UTC/check suite — 2 passed,
container smoke — 1 passed; cleanup удалил только integration containers/network.
Offline suite ранее дал 162 passed, Compose config и Harness — exit 0. Evidence:
`tasks/reports/T08.md`. Production ClickHouse и live БКС не использовались.

После приёмки T08 открыта T09 — независимая итоговая приёмка.

### T09 — принято

Owner `/root/t09_acceptance`. Независимый обзор вернул два дефекта владельцам:
общий CH connect+I/O budget теперь ограничен 15 секундами, а wire DEBUG/INFO noisy
библиотек подавляется при сохранении app DEBUG и redaction WARNING/ERROR. После
исправлений координатор выполнил полный `make verify`: Harness PASS; offline
`165 passed, 3 deselected`; ClickHouse `24.8.4.13` schema/check — 2 passed;
container entrypoint smoke — 1 passed; project containers/network удалены. Все 101
ordered пары совпали с quotes YAML, единственное wire-изменение — dataType 3 → 2.
Evidence: `tasks/reports/T09.md`. Offline MVP принят по R01–R16/S01–S16. S17 —
`not run`, production readiness: unverified.

## 2026-10-17: повторная проверка и передача на удалённый сервер

По новому поручению пользователя координатор проверил runtime и упаковку, вернул
читаемый `python:3.12.13-slim-bookworm`, удалил локальный pip timeout 600 и уточнил
Dockerignore для вложенного bytecode. Integration runner закреплён за отдельным
project и synthetic env; ошибку cleanup нельзя принять за успешный запуск. Добавлены
три проверки test runner и исправлена зависимость dotenv-теста от внешнего env.
Серверный runbook адаптирован под подтверждённый доступ к ClickHouse по IP/DNS/HTTP(S).

Финальный `make verify` — exit 0: Harness PASS; 168 offline passed, 3 integration
deselected в unit; real ClickHouse 24.8.4.13 schema/check — 2 passed, container smoke —
1 passed. Docker-only offline прогон с `--network none` — 168 passed. История
проверок и найденных проблем: `docs/reviews/remote-deployment.md`.

Поскольку до поручения Git-коммитов не было, подготовлен первый коммит всего дерева:
код, спецификации, задачи, отчёты T01–T09 и повторное review. Архив исходников строится
через `git archive HEAD`; серверные секреты, live/production CH, SSH и deploy здесь
не использовались. Runtime-протокол, DDL и 101 пара подписки не менялись.

## Формат записи будущей задачи

Указать task ID, owner, дату, изменённое поведение, команды и фактические exit codes,
evidence paths, ограничения/not run и следующий шаг. Отчёт задачи хранить отдельным
файлом `tasks/reports/Txx.md` после начала выполнения; реестр ссылается на него.
При изменении интерфейса указать затронутые следующие задачи.
