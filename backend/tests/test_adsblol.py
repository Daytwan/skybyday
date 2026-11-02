import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from skytrace.models import TrackingArea
from skytrace.providers.adsblol import AdsbLolProvider, normalize_payload, parse_retry_after
from skytrace.providers.base import ProviderError

NOW = datetime(2026, 10, 18, 18, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "adsblol-synthetic.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_normalizes_units_identity_provenance_and_position_time(payload: dict) -> None:
    batch = normalize_payload(payload, received_at=NOW + timedelta(seconds=1))
    plane = batch.aircraft[0]
    assert plane.icao24 == "00a123"
    assert plane.callsign == "SKY001"
    assert plane.barometric_altitude_m == pytest.approx(3048)
    assert plane.geometric_altitude_m == pytest.approx(3200.4)
    assert plane.geometric_altitude_reference == "wgs84_ellipsoid"
    assert plane.groundspeed_mps == pytest.approx(250 * 1852 / 3600)
    assert plane.barometric_vertical_rate_mps == pytest.approx(-3.048)
    assert plane.geometric_vertical_rate_mps == pytest.approx(-640 * 0.3048 / 60)
    assert plane.ground_track_deg == 90
    assert plane.true_heading_deg == 95
    assert plane.magnetic_heading_deg == 88
    assert plane.roll_deg == -5
    assert plane.pitch_deg is None
    assert plane.position_observed_at == NOW - timedelta(seconds=4)
    assert plane.last_message_at == NOW - timedelta(seconds=3)
    assert plane.provider_timestamp == NOW
    assert plane.received_at == NOW + timedelta(seconds=1)
    assert plane.source == "adsblol"
    assert plane.source_type == "adsb_icao"
    assert batch.source.license == "ODbL-1.0"
    assert batch.raw_record_count == 5
    assert batch.rejected_record_count == 2
    assert {reason.reason: reason.count for reason in batch.rejections} == {
        "invalid_record": 1,
        "non_icao_address": 1,
    }


def test_ground_and_missing_values_are_not_invented(payload: dict) -> None:
    batch = normalize_payload(payload, received_at=NOW)
    ground, metadata = batch.aircraft[1:]
    assert ground.on_ground is True
    assert ground.barometric_altitude_m is None
    assert ground.geometric_altitude_m is None
    assert ground.geometric_altitude_reference is None
    assert metadata.callsign is None
    assert metadata.latitude_deg is None
    assert metadata.longitude_deg is None
    assert metadata.position_observed_at is None
    assert metadata.on_ground is None


def test_position_without_age_is_not_treated_as_current(payload: dict) -> None:
    del payload["ac"][0]["seen_pos"]
    plane = normalize_payload(payload, received_at=NOW).aircraft[0]
    assert plane.latitude_deg is None
    assert plane.longitude_deg is None
    assert plane.position_observed_at is None


@pytest.mark.parametrize("change", ["seconds", "stale", "future", "count", "message", "shape"])
def test_bad_or_stale_envelope_is_not_an_empty_success(payload: dict, change: str) -> None:
    if change == "seconds":
        payload["now"] //= 1000
    elif change == "stale":
        payload["now"] -= 91_000
    elif change == "future":
        payload["now"] += 6_000
    elif change == "count":
        payload["total"] = 0
    elif change == "message":
        payload["msg"] = "upstream unavailable"
    else:
        payload["ac"] = {}
    with pytest.raises(ProviderError):
        normalize_payload(payload, received_at=NOW)


def test_empty_is_valid_but_entirely_rejected_is_not(payload: dict) -> None:
    payload["ac"] = [{"hex": "~123456", "type": "tisb_other"}]
    payload["total"] = 1
    with pytest.raises(ProviderError, match="No valid aircraft"):
        normalize_payload(payload, received_at=NOW)
    payload["ac"] = []
    payload["total"] = 0
    assert normalize_payload(payload, received_at=NOW).aircraft == ()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "39.9", True])
def test_invalid_coordinates_are_counted_not_coerced(payload: dict, bad: object) -> None:
    payload["ac"][0]["lat"] = bad
    batch = normalize_payload(payload, received_at=NOW)
    assert batch.rejected_record_count == 3
    assert "00a123" not in {plane.icao24 for plane in batch.aircraft}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120),
        ("Sun, 18 Oct 2026 18:02:00 GMT", 120),
        ("Sun, 18 Oct 2026 17:00:00 GMT", 0),
        ("-1", None),
        ("garbage", None),
    ],
)
def test_retry_after_forms(header: str, expected: float | None) -> None:
    assert parse_retry_after(header, NOW) == expected


@pytest.mark.anyio
async def test_fetch_uses_one_point_request_and_retains_response_metadata(payload: dict) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload, headers={"Date": "Sun, 18 Oct 2026 18:00:00 GMT"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AdsbLolProvider(client, clock=lambda: NOW)
        batch = await provider.fetch(TrackingArea())
    assert len(requests) == 1
    assert requests[0].url == "https://api.adsb.lol/v2/point/39.8561/-104.6737/60"
    assert batch.http.status_code == 200
    assert batch.http.server_date == "Sun, 18 Oct 2026 18:00:00 GMT"
    assert json.loads(provider.last_response.body) == payload


@pytest.mark.anyio
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (403, False)])
async def test_http_errors_do_not_retry_inside_adapter(status: int, retryable: bool) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "1800"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AdsbLolProvider(client, clock=lambda: NOW)
        with pytest.raises(ProviderError) as caught:
            await provider.fetch(TrackingArea())
    assert calls == 1
    assert caught.value.retryable is retryable
    assert caught.value.retry_after_seconds == 1800


@pytest.mark.anyio
async def test_timeout_becomes_retryable_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError) as caught:
            await AdsbLolProvider(client).fetch(TrackingArea())
    assert caught.value.retryable


@pytest.mark.anyio
@pytest.mark.parametrize("body", [b"{broken", b"x" * 1025])
async def test_malformed_and_oversized_responses_fail(body: bytes) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=body, headers={"Content-Type": "application/json"}
            )
        )
    ) as client:
        with pytest.raises(ProviderError):
            await AdsbLolProvider(client, max_response_bytes=1024).fetch(TrackingArea())
