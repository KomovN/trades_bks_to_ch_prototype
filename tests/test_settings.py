from __future__ import annotations

from pathlib import Path

import pytest

from app.settings import Settings, SettingsError, load_settings


@pytest.fixture
def valid_env() -> dict[str, str]:
    return {"REFRESH_TOKEN": "refresh-sentinel"}


def test_defaults_are_loaded_and_secrets_are_hidden(
    valid_env: dict[str, str], tmp_path: Path
) -> None:
    config = tmp_path / "subscribe_msg.yaml"
    config.write_text("placeholder", encoding="utf-8")
    valid_env["SUBSCRIBE_CONFIG"] = config.name
    valid_env["ACCESS_TOKEN"] = "access-sentinel"
    valid_env["CLICKHOUSE_PASSWORD"] = "password-sentinel"

    settings = Settings.from_env(valid_env, base_dir=tmp_path)

    assert settings.subscribe_config == config
    assert settings.access_token == "access-sentinel"
    assert settings.refresh_token == "refresh-sentinel"
    assert settings.clickhouse_secure is False
    assert settings.batch_size == 1000
    rendered = repr(settings)
    assert "access-sentinel" not in rendered
    assert "refresh-sentinel" not in rendered
    assert "password-sentinel" not in rendered


def test_at_least_one_token_is_required(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="ACCESS_TOKEN or REFRESH_TOKEN") as caught:
        Settings.from_env({}, base_dir=tmp_path)

    assert "sentinel" not in str(caught.value)


@pytest.mark.parametrize("raw", ["1", "yes", "TRUE", "False", "", " false"])
def test_clickhouse_secure_is_a_strict_bool(
    raw: str, valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env["CLICKHOUSE_SECURE"] = raw

    with pytest.raises(SettingsError, match="CLICKHOUSE_SECURE"):
        Settings.from_env(valid_env, base_dir=tmp_path)


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf"])
def test_non_finite_floats_are_rejected(
    raw: str, valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env["FLUSH_INTERVAL_SEC"] = raw

    with pytest.raises(SettingsError, match="FLUSH_INTERVAL_SEC"):
        Settings.from_env(valid_env, base_dir=tmp_path)


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("BATCH_SIZE", "0"),
        ("QUEUE_MAXSIZE", "-1"),
        ("CLICKHOUSE_PORT", "65536"),
        ("ACCESS_TOKEN_MAX_AGE_SEC", "0"),
        ("AUTH_FAILED_EXIT_DELAY_SEC", "-1"),
        ("CLICKHOUSE_CONNECT_TIMEOUT_SEC", "10.1"),
        ("CLICKHOUSE_IO_TIMEOUT_SEC", "11"),
        ("SHUTDOWN_TIMEOUT_SEC", "10"),
    ],
)
def test_limits_are_validated(
    key: str, raw: str, valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env[key] = raw

    with pytest.raises(SettingsError, match=key):
        Settings.from_env(valid_env, base_dir=tmp_path)


def test_clickhouse_request_budget_accepts_exactly_fifteen_seconds(
    valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env.update(
        {
            "CLICKHOUSE_CONNECT_TIMEOUT_SEC": "10",
            "CLICKHOUSE_IO_TIMEOUT_SEC": "5",
        }
    )

    settings = Settings.from_env(valid_env, base_dir=tmp_path)

    assert (
        settings.clickhouse_connect_timeout_sec
        + settings.clickhouse_io_timeout_sec
        == 15
    )


def test_clickhouse_request_budget_rejects_more_than_fifteen_seconds(
    valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env.update(
        {
            "CLICKHOUSE_CONNECT_TIMEOUT_SEC": "10",
            "CLICKHOUSE_IO_TIMEOUT_SEC": "5.001",
        }
    )

    with pytest.raises(
        SettingsError,
        match=r"CLICKHOUSE_CONNECT_TIMEOUT_SEC \+ CLICKHOUSE_IO_TIMEOUT_SEC must be <= 15",
    ):
        Settings.from_env(valid_env, base_dir=tmp_path)


@pytest.mark.parametrize("database", ["db-name", "1db", "db.name", "db name"])
def test_database_must_be_a_safe_identifier(
    database: str, valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env["CLICKHOUSE_DATABASE"] = database

    with pytest.raises(SettingsError, match="CLICKHOUSE_DATABASE"):
        Settings.from_env(valid_env, base_dir=tmp_path)


def test_missing_subscription_file_is_rejected(
    valid_env: dict[str, str], tmp_path: Path
) -> None:
    valid_env["SUBSCRIBE_CONFIG"] = "missing.yaml"

    with pytest.raises(SettingsError, match="SUBSCRIBE_CONFIG"):
        Settings.from_env(valid_env, base_dir=tmp_path)


def test_secret_value_is_absent_from_validation_error(tmp_path: Path) -> None:
    sentinel = "super-secret-sentinel"
    env = {
        "ACCESS_TOKEN": sentinel,
        "SUBSCRIBE_CONFIG": "missing.yaml",
    }

    with pytest.raises(SettingsError) as caught:
        Settings.from_env(env, base_dir=tmp_path)

    assert sentinel not in str(caught.value)
    assert sentinel not in repr(caught.value)


def test_plain_ws_is_allowed_only_on_loopback(
    valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env["WS_URL"] = "ws://localhost:8765/feed"
    assert Settings.from_env(valid_env, base_dir=tmp_path).ws_url.startswith("ws://")

    valid_env["WS_URL"] = "ws://example.com/feed"
    with pytest.raises(SettingsError, match="WS_URL"):
        Settings.from_env(valid_env, base_dir=tmp_path)


def test_reconnect_max_must_not_be_below_min(
    valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "subscribe_msg.yaml").write_text("placeholder", encoding="utf-8")
    valid_env.update(
        {"RECONNECT_MIN_DELAY_SEC": "5", "RECONNECT_MAX_DELAY_SEC": "4"}
    )

    with pytest.raises(SettingsError, match="RECONNECT_MAX_DELAY_SEC"):
        Settings.from_env(valid_env, base_dir=tmp_path)


def test_dotenv_is_loaded_and_process_env_has_priority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "subscription.yaml"
    config.write_text("placeholder", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "REFRESH_TOKEN=file-secret\n"
        "SOURCE=from-file\n"
        "SUBSCRIBE_CONFIG=subscription.yaml\n",
        encoding="utf-8",
    )
    # Isolate the scenario from caller/container defaults and real credentials.
    monkeypatch.setattr("app.settings.os.environ", {"SOURCE": "from-process"})

    settings = load_settings(env_file)

    assert settings.source == "from-process"
    assert settings.refresh_token == "file-secret"
    assert settings.subscribe_config == config
