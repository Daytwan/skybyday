"""Application factory: one shared client and ingestion task per lifespan."""

import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

import httpx
from fastapi import FastAPI

from skytrace.api import router
from skytrace.config import Settings
from skytrace.history import HistoryStore
from skytrace.providers.adsblol import AdsbLolProvider
from skytrace.providers.base import AircraftProvider
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.airlabs_client import AirLabsClient
from skytrace.services.airlabs_context import AirLabsFlightContextService as ActiveContexts
from skytrace.services.airlabs_models import AirLabsFlightContextService
from skytrace.services.airlabs_quota import AirLabsQuota
from skytrace.services.ingestion import IngestionService
from skytrace.services.routes import RouteService
from skytrace.services.runtime import TelemetryRuntime
from skytrace.services.weather import WeatherService


def _validated_airlabs_ledger(path: Path | None) -> Path:
    """Linux local mounts only; unknown filesystem types/platforms fail closed.

    The entire source checkout is excluded conservatively, including the frontend
    source and built served trees. Resolve before checking to reject aliases.
    """
    if path is None or not path.is_absolute() or path.is_symlink():
        raise ValueError("Unavailable accounting")
    resolved = path.resolve(strict=True)
    checkout = Path(__file__).resolve().parents[3]
    info = resolved.stat()
    if (
        resolved.is_relative_to(checkout)
        or not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or not info.st_mode & 0o222
        or resolved.parent.stat().st_mode & 0o022
    ):
        raise ValueError("Unprotected accounting")
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields, filesystem = line.split(" - ", 1)
        mount = Path(fields.split()[4].replace(r"\040", " ").replace(r"\134", "\\"))
        if resolved.is_relative_to(mount):
            mounts.append((len(mount.parts), filesystem.split()[0]))
    if not mounts or max(mounts)[1] not in {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "f2fs",
        "zfs",
        "tmpfs",
    }:
        raise ValueError("Local accounting required")
    return resolved


def create_app(
    settings: Settings | None = None,
    *,
    provider: AircraftProvider | None = None,
    airlabs_service: AirLabsFlightContextService | ActiveContexts | None = None,
    airlabs_activation_approved: bool = False,
) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            httpx.AsyncClient(limits=httpx.Limits(max_connections=2)) as metadata_client,
            httpx.AsyncClient(
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)
            ) as client,
        ):
            selected = provider
            if selected is None:
                selected = (
                    AdsbLolProvider(
                        client, max_snapshot_age_seconds=settings.max_snapshot_age_seconds
                    )
                    if settings.provider == "adsblol"
                    else SyntheticProvider()
                )
            history = HistoryStore(settings)
            contexts = (
                airlabs_service
                if airlabs_service is not None
                else (AirLabsFlightContextService(settings.airlabs_mode))
            )
            runtime = TelemetryRuntime(
                IngestionService(selected, settings),
                history=history,
                airlabs_snapshot_observer=contexts.observe_snapshot,
            )
            if (
                airlabs_service is None
                and settings.airlabs_mode == "selected"
                and airlabs_activation_approved
            ):
                # Approval asserts external operator gates; configuration alone never activates.
                try:
                    key = settings.airlabs_api_key
                    if key is None or not key.get_secret_value().strip():
                        raise ValueError("Missing secret injection")
                    ledger = _validated_airlabs_ledger(settings.airlabs_ledger_path)
                    contexts = ActiveContexts(
                        "selected",
                        snapshot=runtime.snapshot,
                        client_factory=lambda: AirLabsClient(key),
                        quota_factory=lambda: AirLabsQuota(ledger),
                    )
                except Exception:
                    pass  # Safe inert selected projection; never log local paths or secrets.
            runtime.airlabs_snapshot_observer = contexts.observe_snapshot
            runtime.airlabs_observer_failure = getattr(contexts, "observer_failed", None)
            app.state.flight_contexts = contexts
            app.state.runtime = runtime
            app.state.history = history
            app.state.http_client = client
            app.state.weather = WeatherService(metadata_client)
            app.state.routes = RouteService(metadata_client)
            try:
                history.start()
                try:
                    contexts.start()
                except Exception:
                    # An injected optional owner can fail too. Route through a safe
                    # inert owner, while retaining the failed owner for final cleanup.
                    runtime.airlabs_snapshot_observer = None
                    runtime.airlabs_observer_failure = None
                    app.state.flight_contexts = AirLabsFlightContextService(settings.airlabs_mode)
                if isinstance(contexts, ActiveContexts) and not contexts._running:
                    runtime.airlabs_snapshot_observer = None
                    runtime.airlabs_observer_failure = None
                    app.state.flight_contexts = AirLabsFlightContextService(settings.airlabs_mode)
                runtime.start()
                yield
            finally:
                try:
                    await runtime.stop()
                finally:
                    try:
                        await contexts.stop()
                    finally:
                        try:
                            await history.stop()
                        finally:
                            app.state.routes.close()

    app = FastAPI(
        title="SkybyDay",
        version=version("skytrace"),
        description="Shared live ADSB.lol ingestion; explicit synthetic mode for offline use.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.include_router(router)
    return app
