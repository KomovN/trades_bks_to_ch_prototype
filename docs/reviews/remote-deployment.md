# Проверка перед передачей на другой сервер

Дата: 2026-10-17. Выполнил координатор по повторному поручению пользователя.

## Поручение и изменения

- Просмотрены runtime-модули, настройки, auth, parser, writer, reader, supervisor,
  Docker/Compose, init, тестовый runner и инструкция запуска.
- Удалён digest из Python base: теперь `python:3.12.13-slim-bookworm`.
  Тег тестового CH остаётся читаемым `clickhouse/clickhouse-server:24.8.4.13`;
  архитектура контейнеров не принуждается к архитектуре Mac.
- Удалён test-stage pip timeout 600 секунд: это был обход сетевой проблемы локальной
  проверки, а не требование к сервису. Исторический T08 report описывает прежний запуск.
- Исправлен риск cleanup: integration runner больше не берёт имя project из общего
  `COMPOSE_PROJECT_NAME`. Даже при таком env все операции относятся только к
  `trades-bks-to-ch-integration`; volume не удаляется. Ошибка cleanup теперь даёт
  nonzero и не маскирует исходную ошибку тестов.
- Runner явно передаёт synthetic `compose-safe.env`, поэтому Compose не загружает
  серверный `.env` для integration-проверки.
- Добавлены regression tests для изоляции project и кодов выхода runner.
- Test-stage содержит synthetic parser fixtures и подписку, необходимые для полного
  offline suite внутри Docker без доступа к сети; runtime-stage от этого не зависит.
- Dockerignore рекурсивно исключает вложенный Python bytecode. Linux-прогон также
  выявил зависимость dotenv-теста от внешнего `SUBSCRIBE_CONFIG`; тест теперь
  использует контролируемое окружение без credentials вызывающего процесса.
- Runbook переписан для Linux-сервера с существующим CH по IP/DNS и HTTP(S), как
  уточнил пользователь: доставка исходников, серверный `.env`, build на сервере,
  отдельный DDL init, диагностика, запуск, остановка, обновление и rollback.

Пользовательский DDL, 101 пара подписки, требования доставки и runtime-протокол
не изменялись. Секреты сервера не читались; live БКС, production CH и SSH здесь не
использовались. Внешние сетевые обращения проверки ограничены Docker registry/PyPI;
runtime integration использует локальные fake WS/credentials и тестовый CH.

## Фиксация и передача

До этого поручения репозиторий не содержал ни одного Git-коммита; все T01–T09
находились в рабочих файлах. Поручение «зафиксируй всё» включает первый коммит
реализации, спецификаций, отчётов и этой повторной проверки. Удалённый push и deploy
в это поручение не входят.

Первый `git diff --cached --check` выявил лишние пустые строки в конце ряда файлов;
они удалены. Прежние `git diff --check` при полностью untracked-дереве не проверяли
эти файлы. При финальной фиксации проверяется именно staged diff.

Архив `artifacts/trades-bks-to-ch-0.1.0.tar.gz` формируется через `git archive HEAD`:
в него попадают только зафиксированные исходники/документация, без `.env`, кешей,
`.git` и самого архива. Для повторного формирования:

```bash
mkdir -p artifacts
git archive --format=tar.gz --output=artifacts/trades-bks-to-ch-0.1.0.tar.gz HEAD
```

## Результаты проверки

- `PYTHON=<CPython 3.12.13> make verify` — exit 0: Harness PASS; host offline
  `168 passed, 3 deselected in 32.80s`; ClickHouse `24.8.4.13` init успешен;
  schema/check `2 passed in 0.41s`, container entrypoint smoke `1 passed in 0.63s`.
  Cleanup удалил только integration containers/network, test volume сохранён.
- `docker build --target tests -t trades-bks-to-ch-tests:0.1.0 .` — exit 0 с
  читаемым Python tag и стандартным pip timeout.
- `docker run --rm --network none trades-bks-to-ch-tests:0.1.0 python -m pytest
  tests -m 'not integration and not live' -q` — exit 0,
  `168 passed, 3 deselected in 32.53s` в Linux-контейнере без внешней сети.
  В первом таком прогоне dotenv-тест зависел от image env: `1 failed, 167 passed`;
  после изоляции окружения весь набор прошёл повторно.
- Адресная проверка settings/runner — `34 passed`; `sh -n`, `compileall`, оба
  Compose config с synthetic env — exit 0.
- Финальный staged diff проверяется через `git diff --cached --check`; список
  файлов не содержит настоящих `.env`, credentials, кешей или PDF-источника.

Runtime-модули сервиса в этом review не изменены. Предыдущая T09 остаётся историческим
evidence. Live/production readiness по-прежнему unverified до запуска S17 на сервере.
