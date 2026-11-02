from pathlib import Path

import pytest
from pydantic import ValidationError

from skytrace.config import Settings


def test_den_defaults() -> None:
    settings = Settings()
    assert settings.provider == "adsblol"
    assert settings.area_latitude_deg == 39.8561
    assert settings.area_longitude_deg == -104.6737
    assert settings.area_radius_nm == 60
    assert settings.coverage_preset == "den"
    assert settings.poll_interval_seconds == 30
    assert settings.history_enabled is False
    assert settings.history_database_url is None
    assert settings.history_retention_days == 7


def test_synthetic_requires_explicit_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKYTRACE_PROVIDER", "synthetic")
    assert Settings().provider == "synthetic"
    assert Settings(provider="adsblol").provider == "adsblol"


def test_environment_overrides_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SKYTRACE_POLL_INTERVAL_SECONDS=45\n", encoding="utf-8")
    assert Settings().poll_interval_seconds == 45
    monkeypatch.setenv("SKYTRACE_POLL_INTERVAL_SECONDS", "60")
    assert Settings().poll_interval_seconds == 60


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AREA_LATITUDE_DEG", "91"),
        ("AREA_LONGITUDE_DEG", "-181"),
        ("AREA_LATITUDE_DEG", "NaN"),
        ("AREA_LONGITUDE_DEG", "inf"),
        ("AREA_RADIUS_NM", "0"),
        ("AREA_RADIUS_NM", "251"),
        ("AREA_RADIUS_NM", "2.5"),
        ("POLL_INTERVAL_SECONDS", "1"),
        ("POLL_INTERVAL_SECONDS", "NaN"),
        ("POLL_INTERVAL_SECONDS", "never"),
        ("PROVIDER", "opensky"),
        ("MAX_AIRCRAFT", "0"),
        ("MAX_AIRCRAFT", "10001"),
        ("MAX_SNAPSHOT_AGE_SECONDS", "NaN"),
    ],
)
def test_invalid_environment_fails_validation(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(f"SKYTRACE_{name}", value)
    with pytest.raises(ValidationError):
        Settings()


def test_unknown_dotenv_setting_is_not_silently_ignored(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SKYTRACE_AREA_RADUIS_NM=60\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        Settings()


def test_stale_threshold_must_precede_expiry() -> None:
    with pytest.raises(ValidationError, match="stale_after_seconds"):
        Settings(stale_after_seconds=300, expire_after_seconds=300)


def test_world_hubs_requires_a_cycle_aware_stale_threshold() -> None:
    with pytest.raises(ValidationError, match="world_hubs_v1 requires"):
        Settings(coverage_preset="world_hubs_v1")
    settings = Settings(
        coverage_preset="world_hubs_v1",
        stale_after_seconds=420,
        expire_after_seconds=900,
    )
    assert settings.coverage_preset == "world_hubs_v1"


def test_enabled_history_requires_valid_postgres_url() -> None:
    with pytest.raises(ValidationError, match="history_database_url"):
        Settings(history_enabled=True)
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(history_database_url="https://example.test/database")
    configured = Settings(
        history_enabled=True,
        history_database_url="postgresql://user:secret@localhost/skytrace",
    )
    assert configured.history_database_url is not None
    assert "secret" not in repr(configured)
    unix_socket = Settings(
        history_enabled=True,
        history_database_url=(
            "postgresql://skytrace@/skytrace_test?host=/tmp/skytrace-postgis/socket&port=55432"
        ),
    )
    assert unix_socket.history_enabled is True


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "https://example.org/path",
        "https://user@example.org",
        "ws://localhost:8000",
        "http://localhost:bad",
    ],
)
def test_websocket_origins_are_exact_http_origins(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(ws_allowed_origins=(origin,))


def test_websocket_origin_list_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKYTRACE_WS_ALLOWED_ORIGINS", '["https://skytrace.example"]')
    assert Settings().ws_allowed_origins == ("https://skytrace.example",)


def test_airlabs_defaults_empty_examples_and_secret_exclusion(monkeypatch, tmp_path):
    assert Settings().airlabs_mode == "off"
    for name in ("AIRLABS_API_KEY", "AIRLABS_LEDGER_PATH"):
        monkeypatch.setenv(f"SKYTRACE_{name}", "")
    assert Settings().airlabs_api_key is None
    assert Settings().airlabs_ledger_path is None
    settings = Settings(airlabs_api_key="artificial-placeholder")
    assert "artificial-placeholder" not in repr(settings)
    assert "airlabs_api_key" not in settings.model_dump()
    assert "airlabs_api_key" not in settings.model_dump_json()
    ledger = tmp_path / "missing-parent" / "ledger.sqlite"
    selected = Settings(airlabs_mode="selected", airlabs_ledger_path=ledger)
    assert selected.airlabs_ledger_path == ledger
    assert not ledger.parent.exists()  # Syntax validation never creates or inspects it.
    with pytest.raises(ValidationError):
        Settings(airlabs_ledger_path="relative.sqlite")
    with pytest.raises(ValidationError):
        Settings(airlabs_mode="bulk")


def test_airlabs_secret_file_is_not_loaded(monkeypatch, tmp_path):
    import builtins

    secret = tmp_path / ".env.airlabs"
    secret.write_text("SKYTRACE_AIRLABS_MODE=invalid\n", encoding="utf-8")
    original_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        assert str(file) != str(secret) and str(file) != ".env.airlabs"
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    assert Settings().airlabs_mode == "off"
