from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from skytrace.api import fleet_routes
from skytrace.models import AircraftObservation
from skytrace.services.routes import RouteService


@pytest.mark.anyio
async def test_fleet_references_cover_every_identity_without_upstream_requests():
    from starlette.responses import Response

    async def forbidden(_request):
        raise AssertionError("Fleet route lookup must never call an upstream service")

    now = datetime.now(UTC)
    aircraft = [
        AircraftObservation(
            icao24=f"{index:06x}", source="adsblol", source_type="adsb_icao",
            callsign=callsign, provider_timestamp=now, received_at=now,
        )
        for index, callsign in enumerate(["UAL123", "N12345", None], 1)
    ]
    message = SimpleNamespace(
        stream_id="test-stream", sequence=7,
        data=SimpleNamespace(aircraft=aircraft, emitted_at=now),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        service = RouteService(client)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            runtime=SimpleNamespace(snapshot=lambda: message), routes=service,
        )))
        response = Response()
        try:
            result = await fleet_routes(request, response)
            assert result.tracked_aircraft == 3
            assert len(result.references) == 3
            assert result.unknown == 2
            assert result.two_endpoint_references + result.multi_leg_references == 1
            assert result.confirmed_current_flights == 0
            assert result.stream_id == "test-stream"
            assert response.headers["cache-control"] == "no-store"
            assert all(not row.route.current_flight_confirmed for row in result.references)
        finally:
            service.close()
