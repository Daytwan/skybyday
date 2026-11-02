import httpx
import pytest

from skytrace.services.route_data import RouteReferenceIndex
from skytrace.services.routes import RouteService, normalize_vrs_callsign


def test_vrs_keeps_one_numeric_digit_when_stripping_zeroes_before_suffix() -> None:
    assert normalize_vrs_callsign("EZY00AB") == "EZY0AB"


@pytest.mark.anyio
async def test_rate_limit_without_retry_after_uses_bounded_provider_cooldown() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = RouteService(client, reference_index=RouteReferenceIndex.empty())
        limited = await service.get("UAL123")
        waiting = await service.get("DAL456")

    assert limited.reason == "rate_limited"
    assert limited.retry_in_seconds >= 299
    assert waiting.reason == "cooldown"
    assert waiting.retry_in_seconds >= 299


@pytest.mark.anyio
async def test_route_request_identifies_current_product_name() -> None:
    user_agents: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        user_agents.append(request.headers["user-agent"])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await RouteService(client, reference_index=RouteReferenceIndex.empty()).get("UAL123")

    assert user_agents == ["SkybyDay/0.1 (self-hosted aviation explorer)"]
