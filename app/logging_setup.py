"""Настройка компактных логов с обязательным удалением известных секретов."""

from __future__ import annotations

from collections.abc import Iterable
import logging
import re
import sys
from typing import Any


_AUTH_RE = re.compile(r"(?i)(bearer\s+)[^\s,;]+")
_TOKEN_RE = re.compile(
    r"(?i)((?:access|refresh)[_-]?token\s*[=:]\s*)[^\s,;]+"
)
_NOISY_THIRD_PARTY_LOGGERS = (
    "websockets",
    "httpx",
    "httpcore",
    "clickhouse_connect",
    "urllib3",
)


def _is_noisy_third_party(name: str) -> bool:
    return any(
        name == namespace or name.startswith(f"{namespace}.")
        for namespace in _NOISY_THIRD_PARTY_LOGGERS
    )


class ThirdPartyLevelFilter(logging.Filter):
    """Не пропускать wire/debug записи сетевых библиотек в root handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _is_noisy_third_party(record.name) or record.levelno >= logging.WARNING


class SecretRedactionFilter(logging.Filter):
    """Очищать message и exception text до передачи formatter-у."""

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        super().__init__()
        self._secrets = tuple(
            sorted(
                {value for value in secrets if isinstance(value, str) and value},
                key=len,
                reverse=True,
            )
        )

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        if record.exc_info:
            rendered = f"{rendered}: {record.exc_info[1]}"
        record.msg = self.redact(rendered)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True

    def redact(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        value = _AUTH_RE.sub(r"\1[REDACTED]", value)
        return _TOKEN_RE.sub(r"\1[REDACTED]", value)


def configure_logging(
    level: str,
    *,
    secrets: Iterable[str | None] = (),
    stream: Any = None,
) -> logging.Handler:
    """Настроить единственный root handler и вернуть его для тестов/cleanup."""

    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    # Фильтр handler-а остаётся последней защитой, даже если конкретный дочерний
    # logger библиотеки позднее сам выставит DEBUG.
    handler.addFilter(ThirdPartyLevelFilter())
    handler.addFilter(SecretRedactionFilter(secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("app").setLevel(level)
    for namespace in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(namespace).setLevel(logging.WARNING)
    # Некоторые клиенты создают именованные дочерние logger-ы до настройки root и
    # могут выставить им собственный DEBUG. Нормализуем уже существующие экземпляры.
    for name, candidate in logging.root.manager.loggerDict.items():
        if _is_noisy_third_party(name) and isinstance(candidate, logging.Logger):
            candidate.setLevel(logging.WARNING)
    return handler
