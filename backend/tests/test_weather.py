import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from skytrace.services.weather import WeatherService, normalize_weather


def observation(now=None):
    return {
        "properties": {
            "stationId": "KDEN",
            "timestamp": (now or datetime.now(UTC)).isoformat(),
            "temperature": {"unitCode": "wmoUnit:degC", "value": 0},
            "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 36},
            "windDirection": {"unitCode": "wmoUnit:degree_(angle)", "value": 0},
            "visibility": {"unitCode": "wmoUnit:m", "value": None},
            "cloudLayers": [],
        }
    }


def test_weather_units_missing_fields_and_future_clock():
    now = datetime.now(UTC)
    value = normalize_weather(observation(now), now)
    assert value.temperature_c == 0
    assert value.wind_speed_mps == 10
    assert value.wind_direction_deg == 0
    assert value.visibility_m is None and value.cloud_base_m is None
    assert value.description == "Conditions not reported"
    with pytest.raises(ValueError):
        normalize_weather(observation(now + timedelta(hours=1)), now)
    bad = observation(now)
    bad["properties"]["windSpeed"]["unitCode"] = "unknown"
    assert normalize_weather(bad, now).wind_speed_mps is None
    bad = observation(now)
    bad["properties"]["windSpeed"]["qualityControl"] = []
    assert normalize_weather(bad, now).wind_speed_mps is None


@pytest.mark.parametrize("cover", ["CLR", "SKC", ["OVC"], {"invalid": True}])
def test_clear_sky_limits_and_malformed_cloud_cover_are_not_measurements(cover):
    now = datetime.now(UTC)
    value = observation(now)
    value["properties"]["cloudLayers"] = [
        {"amount": cover, "base": {"unitCode": "wmoUnit:m", "value": 3657}}
    ]
    result = normalize_weather(value, now)
    if isinstance(cover, str):
        assert result.cloud_cover == cover
        assert result.cloud_base_m is None
    else:
        assert result.cloud_cover is None


@pytest.mark.anyio
async def test_ten_weather_viewers_share_one_fetch_and_throttling_keeps_old_data():
    count = 0

    async def respond(request):
        nonlocal count
        count += 1
        assert request.headers["user-agent"].startswith("SkybyDay/")
        if count > 1:
            return httpx.Response(429, headers={"Retry-After": "7200"})
        return httpx.Response(200, json=observation(datetime.now(UTC) - timedelta(hours=2)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = WeatherService(client)
        results = await asyncio.gather(*(service.get() for _ in range(10)))
        assert count == 1
        assert all(x.status == "available" and x.stale for x in results)
        service.next_fetch = 0
        result = await service.get()
        assert result.status == "degraded" and result.observation is not None
        assert result.retry_in_seconds >= 7199
        await service.get()
        assert count == 2


@pytest.mark.anyio
async def test_weather_bad_response_has_no_substitute():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 262145))
    ) as client:
        result = await WeatherService(client).get()
        assert result.status == "unavailable" and result.observation is None
