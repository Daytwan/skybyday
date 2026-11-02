"""Validated configuration for the shared local ingestion service."""

from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SKYTRACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    area_latitude_deg: float = Field(default=39.8561, ge=-90, le=90)
    area_longitude_deg: float = Field(default=-104.6737, ge=-180, le=180)
    area_radius_nm: int = Field(default=60, ge=1, le=250)
    coverage_preset: Literal["den", "world_hubs_v1"] = "den"
    poll_interval_seconds: float = Field(default=30, ge=30, le=3600)
    provider: Literal["synthetic", "adsblol"] = "adsblol"
    max_aircraft: int = Field(default=2000, ge=1, le=10_000)
    stale_after_seconds: float = Field(default=90, ge=1, le=3600)
    expire_after_seconds: float = Field(default=300, ge=1, le=86400)
    max_snapshot_age_seconds: float = Field(default=90, ge=1, le=3600)
    ws_max_clients: int = Field(default=32, ge=1, le=256)
    ws_send_timeout_seconds: float = Field(default=5, ge=0.1, le=30)
    ws_allowed_origins: tuple[str, ...] = Field(
        default=tuple(
            f"http://{host}:{port}"
            for host in ("127.0.0.1", "localhost")
            for port in (5173, 4173, 8000)
        ),
        min_length=1,
        max_length=32,
    )
    airlabs_mode: Literal["off", "selected"] = "off"
    airlabs_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    airlabs_ledger_path: Path | None = None
    history_enabled: bool = False
    history_database_url: SecretStr | None = None
    history_schema: str = Field(default="skytrace_history", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    history_retention_days: int = Field(default=7, ge=1, le=365)

    @field_validator("airlabs_api_key", "airlabs_ledger_path", mode="before")
    @classmethod
    def empty_airlabs_placeholder(cls, value):
        return None if value == "" else value

    @field_validator("airlabs_ledger_path")
    @classmethod
    def absolute_airlabs_path(cls, value: Path | None) -> Path | None:
        # Syntax only: never inspect/open a ledger or its parent in Stage A.
        if value is not None and not value.is_absolute():
            raise ValueError("airlabs_ledger_path must be absolute")
        return value

    @field_validator("ws_allowed_origins")
    @classmethod
    def exact_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
                or any(character.isspace() for character in origin)
            ):
                raise ValueError("WebSocket origins must be exact HTTP(S) origins without paths")
            _ = parsed.port
        return origins

    @field_validator("history_database_url")
    @classmethod
    def postgres_history_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        parsed = urlsplit(value.get_secret_value())
        socket_hosts = parse_qs(parsed.query).get("host", [])
        has_socket_host = any(host.startswith("/") for host in socket_hosts)
        if parsed.scheme not in ("postgres", "postgresql") or not (
            parsed.hostname or has_socket_host
        ):
            raise ValueError("history_database_url must be a PostgreSQL URL")
        return value

    @model_validator(mode="after")
    def stale_before_expiry(self) -> Self:
        if self.stale_after_seconds >= self.expire_after_seconds:
            raise ValueError("stale_after_seconds must be less than expire_after_seconds")
        if self.history_enabled and self.history_database_url is None:
            raise ValueError("history_database_url is required when history is enabled")
        if self.coverage_preset == "world_hubs_v1":
            # Six point queries share one slot.  The extra 30 seconds permits a
            # completed cycle to survive one delayed slot without false freshness.
            required_stale = 6 * self.poll_interval_seconds * 2 + 30
            if self.stale_after_seconds < required_stale:
                raise ValueError(
                    "world_hubs_v1 requires stale_after_seconds at least "
                    f"{required_stale:g} for its shared polling cycle"
                )
        return self
