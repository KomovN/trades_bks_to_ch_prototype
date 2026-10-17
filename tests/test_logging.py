from __future__ import annotations

from io import StringIO
import logging

from app.logging_setup import SecretRedactionFilter, configure_logging


def test_redaction_filter_removes_known_and_structured_secrets() -> None:
    secret = "sentinel-refresh-secret"
    record = logging.LogRecord(
        "vendor",
        logging.ERROR,
        __file__,
        1,
        "request failed %s Bearer abc.def refresh_token=visible",
        (secret,),
        None,
    )

    assert SecretRedactionFilter([secret]).filter(record)

    rendered = record.getMessage()
    assert secret not in rendered
    assert "abc.def" not in rendered
    assert "visible" not in rendered
    assert rendered.count("[REDACTED]") == 3


def test_configured_root_handler_scrubs_third_party_exception_text() -> None:
    stream = StringIO()
    secret = "sentinel-access-secret"
    handler = configure_logging("INFO", secrets=[secret], stream=stream)
    vendor = logging.getLogger("vendor.client")

    try:
        try:
            raise RuntimeError(f"server echoed {secret}")
        except RuntimeError:
            vendor.exception("library request failed")
    finally:
        logging.getLogger().removeHandler(handler)

    output = stream.getvalue()
    assert "library request failed" in output
    assert secret not in output
    assert "[REDACTED]" in output
    assert "Traceback" not in output


def test_debug_keeps_app_diagnostics_but_suppresses_third_party_wire_data() -> None:
    stream = StringIO()
    raw_frame = 'sentinel-raw-frame {"ticker":"SBER","price":316.5300}'
    raw_header = "sentinel-header Authorization: Bearer wire-secret"
    handler = configure_logging("DEBUG", stream=stream)

    try:
        for namespace in (
            "websockets.client",
            "httpx",
            "httpcore.http11",
            "clickhouse_connect.driver.httpclient",
            "urllib3.connectionpool",
        ):
            # Явный DEBUG дочернего logger-а проверяет handler filter, а не только
            # наследование WARNING от родительского namespace.
            noisy = logging.getLogger(namespace)
            noisy.setLevel(logging.DEBUG)
            noisy.debug("wire frame=%s header=%s", raw_frame, raw_header)
        logging.getLogger("httpx").warning(
            "event=http_warning Authorization: Bearer warning-secret"
        )
        logging.getLogger("app.lifecycle").debug("event=app_debug state=ready")
    finally:
        logging.getLogger().removeHandler(handler)

    output = stream.getvalue()
    assert "event=app_debug state=ready" in output
    assert "sentinel-raw-frame" not in output
    assert "sentinel-header" not in output
    assert "wire-secret" not in output
    assert "wire frame=" not in output
    assert "event=http_warning" in output
    assert "warning-secret" not in output
    assert "Bearer [REDACTED]" in output
