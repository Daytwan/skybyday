import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from skytrace.services import routes
from skytrace.services.route_data import RouteReferenceIndex
from skytrace.services.routes import RouteService, normalize_vrs_callsign


def write_route_index(
    path,
    *,
    retrieved_at: str = "2026-10-23T09:09:54Z",
    airport_codes: str = '["KAAA","KBBB"]',
    airports: tuple[tuple[object, ...], ...] = (),
) -> None:
    """Write the actual minimal SQLite shape used by the immutable reader."""

    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE routes (
                callsign TEXT PRIMARY KEY, airport_codes TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE airports (
                code TEXT PRIMARY KEY, name TEXT NOT NULL, icao TEXT, iata TEXT,
                latitude REAL, longitude REAL
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            (
                ("url", json.dumps("https://example.invalid/vrs")),
                ("commit", json.dumps("abc")),
                ("retrieved_at", json.dumps(retrieved_at)),
            ),
        )
        connection.execute("INSERT INTO routes VALUES (?, ?)", ("UAL1", airport_codes))
        connection.executemany("INSERT INTO airports VALUES (?, ?, ?, ?, ?, ?)", airports)
        connection.commit()
    finally:
        connection.close()


def test_vrs_callsign_normalization_requires_verified_aliases():
    assert normalize_vrs_callsign(" UAL0001 ") == "UAL1"
    assert normalize_vrs_callsign("U21234") is None
    assert normalize_vrs_callsign("U21234", icao_aliases={"U2": "EZY"}) == "EZY1234"
    assert normalize_vrs_callsign("N144CA") is None
    assert normalize_vrs_callsign("UAL12345") is None


def test_route_index_fails_closed_on_malformed_sqlite_route_row(tmp_path):
    path = tmp_path / "route-index.sqlite3"
    write_route_index(path, airport_codes='["KAAA",3]')
    index = RouteReferenceIndex.from_path(path)
    with pytest.raises(ValueError):
        index.lookup("UAL1")
    index.close()


def test_route_index_rejects_naive_snapshot_timestamp(tmp_path):
    path = tmp_path / "route-index.sqlite3"
    write_route_index(path, retrieved_at="2026-10-23T09:09:54")
    with pytest.raises(ValueError):
        RouteReferenceIndex.from_path(path)


def test_route_index_does_not_invent_a_missing_airport_record(tmp_path):
    path = tmp_path / "route-index.sqlite3"
    write_route_index(
        path,
        airports=(("KAAA", "Known Airport", "KAAA", "AAA", 40.0, -105.0),),
    )
    index = RouteReferenceIndex.from_path(path)
    service = RouteService(object(), reference_index=index)
    result = service.resolve_reference("UAL1")
    try:
        assert result.status == "reference"
        assert [airport.icao for airport in result.airports] == ["KAAA", "KBBB"]
        assert result.airports[0].latitude == 40.0
        assert result.airports[1].name is None
        assert result.airports[1].latitude is None
        assert result.airports[1].longitude is None
    finally:
        service.close()


def test_empty_route_index_reports_source_unavailable_to_zero_network_consumers():
    service = RouteService(object(), reference_index=RouteReferenceIndex.empty())
    result = service.resolve_reference("UAL123")
    assert result.status == "unavailable"
    assert result.reason == "source_error"
    assert result.lookup_callsign == "UAL123"


@pytest.mark.anyio
async def test_bundled_global_snapshot_resolves_routes_and_airports_without_network():
    requests = 0

    async def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = RouteService(client)
        direct = service.resolve_reference("UAL123")
        result = await service.get("UAL123")
        absent = await service.get("ZZZ123")

    assert direct == result
    assert result.status == "reference"
    assert result.reason == "available"
    assert result.source == "VRS standing-data snapshot"
    assert result.source_snapshot_commit == "ba4454ca8b96c0302a0e037a644d7504d9b5beb0"
    assert [airport.iata for airport in result.airports] == ["LHR", "EWR"]
    assert result.airports[0].name == "London Heathrow Airport"
    assert result.airports[0].latitude == pytest.approx(51.4706)
    assert result.current_flight_confirmed is False
    assert absent.status == "unknown"
    assert absent.reason == "unmatched"
    assert requests == 0


@pytest.mark.anyio
async def test_route_reference_is_shared_bounded_and_never_confirmed():
    count = 0

    async def respond(_):
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={
                "callsign": "UAL123",
                "_airports": [
                    {"icao": "KDEN", "iata": "DEN", "name": "Denver"},
                    {"icao": "KSFO", "iata": "SFO", "name": "San Francisco"},
                    {"icao": "KLAX", "iata": "LAX", "name": "Los Angeles"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = RouteService(client, reference_index=RouteReferenceIndex.empty())
        results = await asyncio.gather(*(service.get("UAL123") for _ in range(10)))
        assert count == 1
        assert all(
            r.status == "reference"
            and r.reason == "multi_leg_reference"
            and r.lookup_callsign == "UAL123"
            and not r.current_flight_confirmed
            for r in results
        )
        assert len(results[0].airports) == 3  # Keep the chain; do not pick an assumed leg.
        invalid = await service.get("N12345")
        assert invalid.status == "unknown"
        assert invalid.reason == "invalid_callsign"
        assert (await service.get("UAL456")).status == "waiting"
        assert count == 1


@pytest.mark.anyio
async def test_route_mismatch_and_throttle_do_not_become_routes():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"callsign": "WRONG", "_airports": []})
        )
    ) as client:
        unavailable = await RouteService(
            client, reference_index=RouteReferenceIndex.empty()
        ).get("UAL123")
        assert unavailable.status == "unavailable"
        assert unavailable.reason == "malformed"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "1800"})
        )
    ) as client:
        service = RouteService(client, reference_index=RouteReferenceIndex.empty())
        limited = await service.get("UAL123")
        assert limited.airports == []
        assert limited.reason == "rate_limited"
        assert limited.retry_in_seconds >= 1799
        waiting = await service.get("UAL456")
        assert waiting.reason == "cooldown"


@pytest.mark.anyio
async def test_vrs_alias_is_looked_up_by_canonical_key_without_guessing():
    requested: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "callsign": "EZY1234",
                "_airports": [
                    {"icao": "EGLL", "iata": "LHR", "name": "London Heathrow"},
                    {"icao": "KDEN", "iata": "DEN", "name": "Denver"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = RouteService(
            client,
            icao_aliases={"U2": "EZY"},
            reference_index=RouteReferenceIndex.empty(),
        )
        result = await service.get("U21234")

    assert result.status == "reference"
    assert result.reason == "available"
    assert result.callsign == "U21234"
    assert result.lookup_callsign == "EZY1234"
    assert requested == ["https://vrs-standing-data.adsb.lol/routes/EZ/EZY1234.json"]
    cached = await service.get("EZY1234")
    assert cached.callsign == "EZY1234"
    assert cached.lookup_callsign == "EZY1234"
    assert len(requested) == 1


@pytest.mark.anyio
async def test_failure_cache_counts_down_and_repeated_throttling_backs_off(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(routes.random, "uniform", lambda _a, _b: 0)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = RouteService(client, reference_index=RouteReferenceIndex.empty())
        assert (await service.get("UAL123")).retry_in_seconds == 300
        clock[0] += 100
        assert (await service.get("UAL123")).retry_in_seconds == 200
        assert (await service.get("DAL456")).retry_in_seconds == 200
        assert len(requests) == 1
        clock[0] += 200
        assert (await service.get("UAL123")).retry_in_seconds == 600
        assert len(requests) == 2


@pytest.mark.anyio
async def test_retry_after_larger_than_backoff_is_retained_in_cached_errors(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "7200"})
        )
    ) as client:
        service = RouteService(client, reference_index=RouteReferenceIndex.empty())
        assert (await service.get("UAL123")).retry_in_seconds == 7200
        clock[0] += 600
        assert (await service.get("UAL123")).retry_in_seconds == 6600
        assert (await service.get("DAL456")).retry_in_seconds == 6600
