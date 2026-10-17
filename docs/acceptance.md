# Приёмка и трассировка требований

Каждый сценарий — требуемая проверка будущего runtime. Сейчас он не выполнен.
Unit/component используют fake clock/transport/CH client и не ходят в сеть.
Process tests проверяют фактический выход процесса, integration — настоящий локальный CH.
Live smoke учитывается отдельно и никогда не маскируется под offline-тест.

| Сценарий | Требования | Проверка и ожидаемый результат | Уровень |
| --- | --- | --- | --- |
| S01 | R12 | Нет токенов, invalid bool/NaN/negative limits/unsafe identifier/missing YAML → ошибка до сети; секреты отсутствуют в repr/logs. | unit |
| S02 | R01, R12 | YAML загружается как 101 уникальная пара из согласованного snapshot; dataType=2 и subscribeType=0; пустые/дубли/неверные types отклоняются. | unit |
| S03 | R02, R03 | Пробелы, escape, неизвестные поля и 316.5300 в raw остаются точными; timestamp приёма задан до задержки очереди, offset переводится в UTC. | unit |
| S04 | R03, R04 | Naive/invalid/missing time, null/nonstring ticker/classCode отклоняются без замены now и coercion. | unit |
| S05 | R04 | Fixtures покрывают LastTrades и synthetic LastTradesSuccess payload, ack, error с trade fields, partial payload, other, broken JSON и UTF-8. | unit |
| S06 | R05 | Flush на размере и на времени первой строки, остаточный flush; нет пустых INSERT/busy loop, CH вызов не блокирует heartbeat event loop. | component |
| S07 | R06, R10 | CH outage удерживает тот же batch, retry с bounded backoff; full queue даёт backpressure, signal прерывает put, fatal schema/access не зацикливается. | component |
| S08 | R06, R13 | Fake CH «сохранил, но ответ потерян»: повтор содержит те же значения; дубли допустимы и описаны. task_done только после ack, queue.join не завершается раньше. | component |
| S09 | R08, R12 | Static token приоритетен; refresh form корректна; cache и expires_in проверены fake clock, 429/5xx/network и permanent auth различаются, секреты не утекают. | unit |
| S10 | R07, R08 | Долгоживущий WS переподключается до expiry, повторяет подписку; отказ static token завершает цикл, повторный refresh auth не зацикливается. | component |
| S11 | R01, R07 | Disconnect/handshake failure → backoff+jitter+resubscribe; мгновенный close не сбрасывает backoff; NoData без reconnect storm, NotFound/IncorrectJson fatal. | component |
| S12 | R09, R10 | Permanent refresh auth → stop reader, bounded drain, interruptible delay, exit 1; SIGTERM во время delay обрабатывается быстро. | process |
| S13 | R10 | Сигнал при recv/queue.put/retry и гонке producer completion; каждая принятая строка written либо unsaved; CH down → процесс завершён <=45 сек при defaults. | process |
| S14 | R11, R15 | Длительный idle не создаёт строки и не переподключает живой WS; summary редкий и счётчики проверяемы. | component |
| S15 | R02, R03, R13, R14, R16 | В настоящем CH дважды применить DDL, проверить engine/partition/order/primary key/codecs; INSERT/SELECT exact raw, UTC миллисекунды и отдельность одинаковых строк. | integration |
| S16 | R10, R12, R15, R16 | Падение writer/reader замечает supervisor; процесс выходит, остальные workers завершаются; логи исключений библиотек не содержат sentinel secrets. | process |
| S17 | R01, R08, R14 | Реальный БКС: подтверждение подписки полного списка, safe captures, обновление токена/повторная подписка, версия CH и короткий soak. | live |

## Команды и доказательства

- `make check` — целостность Harness; не исполняет runtime-сценарии.
- `make unit` — S01–S14/S16 без внешнего БКС и production ClickHouse.
- `make integration` — S15 на изолированном настоящем CH. Не пропускать suite молча,
  если CH недоступен: явный nonzero/not run в отчёте.
- Live S17 выполняется отдельно, без секретов в CI, только с поручением пользователя.

T09 сверяет все R01–R16 и реальные результаты. Для offline MVP требуются S01–S16;
для заявления о проверенной production-работе дополнительно S17. Throughput target
пока не согласован: короткий soak — измерение, не обещание заданной производительности.

Отчёт содержит команды, exit codes, runtime/Python/CH versions, наблюдаемый результат,
not run и ограничения. Журнал не должен содержать выдуманных результатов тестирования.
