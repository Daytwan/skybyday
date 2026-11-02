import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from skytrace.config import Settings
from skytrace.main import create_app
from skytrace.models import TrackingArea
from skytrace.providers.adsbfi import AdsbFiProvider
from skytrace.providers.base import ProviderError

NOW = datetime(2026, 10, 18, 18, tzinfo=UTC)


def fixture() -> dict:
    # Artificial v2 wire data, never a redistributed ADSB.fi capture.
    return json.loads((Path(__file__).parent / "fixtures/adsblol-synthetic.json").read_text())


@pytest.mark.anyio
async def test_regional_request_preserves_adsbfi_identity_terms_and_observation_time() -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=fixture())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        batch = await AdsbFiProvider(client, clock=lambda: NOW).fetch(TrackingArea())
    assert len(requests) == 1
    assert str(requests[0].url) == (
        "https://opendata.adsb.fi/api/v3/lat/39.8561/lon/-104.6737/dist/60"
    )
    assert batch.source.name == "adsbfi"
    assert batch.source.url == "https://adsb.fi/"
    assert batch.source.license == "Personal, non-commercial use"
    assert batch.source.license_url == "https://github.com/adsbfi/opendata#terms"
    assert all(plane.source == "adsbfi" for plane in batch.aircraft)
    assert (NOW - batch.aircraft[0].position_observed_at).total_seconds() == 4
    assert batch.aircraft[0].geometric_altitude_reference == "wgs84_ellipsoid"


@pytest.mark.anyio
@pytest.mark.parametrize("status,retryable", [(403, False), (429, True)])
async def test_errors_retain_provider_and_retry_policy_without_fallback(status, retryable) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match=f"ADSB.fi returned HTTP {status}") as caught:
            await AdsbFiProvider(client).fetch(TrackingArea())
    assert len(requests) == 1
    assert caught.value.retryable is retryable
    assert caught.value.retry_after_seconds == 120


@pytest.mark.parametrize(
    "settings",
    [
        {"history_enabled": True, "history_database_url": "postgresql://localhost/test"},
        {"airlabs_mode": "selected"},
        {
            "coverage_preset": "world_hubs_v1",
            "stale_after_seconds": 420,
            "expire_after_seconds": 900,
        },
    ],
)
def test_adsbfi_demo_keeps_unverified_integrations_disabled(settings) -> None:
    with pytest.raises(ValidationError, match="ADSB.fi demo requires"):
        Settings(provider="adsbfi", **settings)


@pytest.mark.anyio
async def test_adsbfi_snapshot_and_routes_are_served_without_contacting_adsblol() -> None:
    payload = fixture()
    payload["now"] = payload["ctime"] = int(datetime.now(UTC).timestamp() * 1000)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as upstream:
        app = create_app(Settings(provider="adsbfi"), provider=AdsbFiProvider(upstream))
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(2):
                while app.state.runtime.snapshot().data.status == "idle":
                    await asyncio.sleep(0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                snapshot = (await client.get("/api/aircraft")).json()
                assert snapshot["data"]["source"] == "adsbfi"
                assert snapshot["data"]["status"] == "healthy"
                assert snapshot["data"]["batch"]["source"]["license"] != "ODbL-1.0"
                assert (await client.get("/api/health")).json()["telemetry"] == "adsbfi"
                routes = (await client.get("/api/routes")).json()
                assert routes["tracked_aircraft"] == len(snapshot["data"]["aircraft"])
                assert routes["confirmed_current_flights"] == 0
                assert (await client.get("/api/history/status")).json()["enabled"] is False
