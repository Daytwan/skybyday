import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from skytrace.config import Settings
from skytrace.main import create_app


@pytest.mark.anyio
async def test_health_distinguishes_synthetic_data_and_provider_health(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "skytrace"
    assert payload["version"]
    assert payload["milestone"] == "backend-api"
    assert payload["telemetry"] == "synthetic"
    assert payload["provider_status"] == "healthy"
    assert payload["poller_running"] is True
    assert payload["cached_aircraft_count"] == 2
    assert payload["area"] == {
        "latitude_deg": 39.8561,
        "longitude_deg": -104.6737,
        "radius_nm": 60,
    }
    assert payload["poll_interval_seconds"] == 30


@pytest.mark.anyio
async def test_app_instances_have_independent_configuration(client: AsyncClient) -> None:
    custom = Settings(
        provider="synthetic",
        area_latitude_deg=0,
        area_longitude_deg=0,
        area_radius_nm=10,
        stale_after_seconds=5,
        expire_after_seconds=12,
    )
    app = create_app(custom)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as custom_client:
            assert (await custom_client.get("/api/health")).json()["area"]["latitude_deg"] == 0
            assert (await client.get("/api/health")).json()["area"]["latitude_deg"] == 39.8561
            policy = (await custom_client.get("/api/aircraft")).json()["tracking"]
            assert policy["area"]["radius_nm"] == 10
            assert policy["stale_after_seconds"] == 5
            assert policy["expire_after_seconds"] == 12


@pytest.mark.anyio
async def test_openapi_advertises_implemented_http_routes(client: AsyncClient) -> None:
    assert (await client.get("/api/docs")).status_code == 200
    schema = (await client.get("/api/openapi.json")).json()
    assert set(schema["paths"]) == {
        "/api/health",
        "/api/flight-contexts",
        "/api/weather",
        "/api/routes",
        "/api/aircraft/{icao24}/route",
        "/api/aircraft",
        "/api/aircraft/{icao24}",
        "/api/history/status",
        "/api/history/aircraft/{icao24}",
        "/api/history/positions",
    }
    assert (await client.get("/api/missing")).status_code == 404


@pytest.mark.anyio
async def test_cached_list_detail_and_missing_aircraft(client: AsyncClient) -> None:
    response = await client.get("/api/aircraft")
    assert response.headers["cache-control"] == "no-store"
    message = response.json()
    assert message["type"] == "snapshot"
    assert message["schema_version"] == 1
    assert message["tracking"]["area"]["radius_nm"] == 60
    assert message["tracking"]["stale_after_seconds"] == 90
    assert message["tracking"]["expire_after_seconds"] == 300
    assert len(message["data"]["aircraft"]) == 2
    assert message["data"]["batch"]["source"]["license"] == "MIT"
    detail = await client.get("/api/aircraft/000001")
    assert detail.status_code == 200
    assert detail.json()["aircraft"] == message["data"]["aircraft"][0]
    assert detail.json()["stale"] is False
    assert detail.json()["stream_id"] == message["stream_id"]
    assert (await client.get("/api/aircraft/ffffff")).status_code == 404
    assert (await client.get("/api/aircraft/not-hex")).status_code == 422


@pytest.mark.anyio
async def test_http_reads_do_not_start_a_missing_lifespan() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/aircraft")).status_code == 503


@pytest.mark.anyio
async def test_disabled_history_status_queries_and_validation(client: AsyncClient) -> None:
    status = await client.get("/api/history/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["state"] == "disabled"

    valid = {
        "source": "synthetic",
        "start": "2026-10-19T00:00:00Z",
        "end": "2026-10-19T01:00:00Z",
    }
    unavailable = await client.get("/api/history/aircraft/000001", params=valid)
    assert unavailable.status_code == 503
    assert "postgres" not in unavailable.text.lower()

    invalid = await client.get(
        "/api/history/positions",
        params=valid
        | {
            "end": "2026-10-20T01:00:00Z",
            "latitude_deg": 39.8,
            "longitude_deg": -104.7,
            "radius_m": 1000,
        },
    )
    assert invalid.status_code == 422


@pytest.mark.anyio
async def test_cross_origin_browser_access_is_not_enabled(client: AsyncClient) -> None:
    response = await client.get("/api/health", headers={"Origin": "https://example.org"})
    assert "access-control-allow-origin" not in response.headers


def test_bad_configuration_prevents_application_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKYTRACE_AREA_RADIUS_NM", "999")
    with pytest.raises(ValidationError):
        create_app()


@pytest.fixture
def context_api():
    from types import SimpleNamespace

    from test_airlabs_models import NOW, artificial_snapshot

    from skytrace.services.airlabs_models import AirLabsFlightContextService, FlightContextCommand

    class ArtificialService(AirLabsFlightContextService):
        submissions = 0

        def submit(self, intent, snapshot):
            self.submissions += 1
            return FlightContextCommand(
                stream_id=snapshot.stream_id,
                sequence=snapshot.sequence,
                icao24=intent.icao24,
                callsign=intent.callsign,
                state="queued",
                reason="queued",
            )

    snapshot = artificial_snapshot(400)
    service = ArtificialService(clock=lambda: NOW)
    app = create_app(Settings(provider="synthetic"), airlabs_service=service)
    app.state.runtime = SimpleNamespace(
        snapshot=lambda: snapshot,
        ingestion=SimpleNamespace(clock=lambda: NOW),
    )
    app.state.flight_contexts = service
    return app, service, snapshot


def intent_headers():
    return {
        "Origin": "http://localhost:5173",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Skytrace-Flight-Intent": "lookup",
        "Sec-Fetch-Site": "same-origin",
    }


@pytest.mark.anyio
async def test_context_get_full_denominator_never_submits(context_api):
    app, service, _ = context_api
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for _ in range(3):
            response = await client.get("/api/flight-contexts")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            batch = response.json()
            assert batch["tracked_aircraft"] == batch["counts"]["disabled"] == 400
            assert len(batch["contexts"]) == 400
    assert service.submissions == 0


@pytest.mark.anyio
async def test_context_valid_lower_sequence_reaches_injected_service(context_api):
    from test_airlabs_models import artificial_intent

    app, service, snapshot = context_api
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/flight-contexts",
            json=artificial_intent(snapshot),
            headers=intent_headers(),
        )
    assert response.status_code == 202
    assert response.json()["sequence"] == snapshot.sequence
    assert response.headers["cache-control"] == "no-store"
    assert service.submissions == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://foreign.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:5173,http://localhost:5173"},
        [("Origin", "http://localhost:5173"), ("Origin", "http://localhost:5173")],
        {"Origin": "http://localhost:5173", "Content-Type": "text/plain"},
        {"Origin": "http://localhost:5173", "X-Skytrace-Flight-Intent": "wrong"},
        {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://localhost:5173", "Content-Type": "application/json; charset=latin-1"},
        {"Origin": "http://localhost:5173", "Content-Type": "application/x-www-form-urlencoded"},
    ],
)
async def test_context_header_rejection_precedes_body(context_api, headers):
    app, service, _ = context_api
    # Fully absent headers and duplicate Origin intentionally bypass header defaults.
    if isinstance(headers, dict) and headers:
        headers = intent_headers() | headers
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/flight-contexts", content="bad json", headers=headers)
    assert response.status_code == 403
    assert response.json()["reason"] == "origin_rejected"
    assert response.headers["cache-control"] == "no-store"
    assert service.submissions == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body,reason",
    [
        (b"x" * 2049, "body_too_large"),
        (b'{"schema_version":1,"schema_version":1}', "invalid_request"),
        (b'{"sequence":NaN}', "invalid_request"),
        (b'{"sequence":Infinity}', "invalid_request"),
        (b"\xff", "invalid_request"),
        (b"[1,2]", "invalid_request"),
        (b"{", "invalid_request"),
        (b"{" + b'"x":[' * 350 + b"0" + b"]}" * 350, "body_too_large"),
    ],
)
async def test_context_bounded_safe_json_rejection(context_api, body, reason):
    app, service, _ = context_api
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/flight-contexts", content=body, headers=intent_headers())
    assert response.status_code == 422
    assert response.json()["reason"] == reason
    assert response.json()["context"] is None
    assert response.headers["cache-control"] == "no-store"
    assert service.submissions == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "updates,status",
    [
        ({"sequence": 43}, 409),
        ({"view": "replay"}, 409),
        ({"callsign": "ZZZ002"}, 409),
        ({"stream_id": "other"}, 409),
        ({"position_observed_at": "2026-10-28T00:00:01Z"}, 409),
        ({"extra": "do not echo this secret-like text"}, 422),
        ({"sequence": True}, 422),
        ({"icao24": "not-hex"}, 422),
    ],
)
async def test_context_admission_rejections_do_not_submit(context_api, updates, status):
    from test_airlabs_models import artificial_intent

    app, service, snapshot = context_api
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/flight-contexts",
            json=artificial_intent(snapshot) | updates,
            headers=intent_headers(),
        )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "secret-like" not in response.text
    assert service.submissions == 0


@pytest.mark.anyio
async def test_context_lifecycle_and_disabled_safe_errors(context_api):
    from test_airlabs_models import NOW, artificial_intent

    from skytrace.services.airlabs_models import AirLabsFlightContextService

    app, _, snapshot = context_api
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for mode, reason in [("off", "disabled"), ("selected", "lifecycle_unavailable")]:
            app.state.flight_contexts = AirLabsFlightContextService(mode, clock=lambda: NOW)
            response = await client.post(
                "/api/flight-contexts",
                json=artificial_intent(snapshot),
                headers=intent_headers(),
            )
            assert (response.status_code, response.json()["reason"]) == (503, reason)
            assert response.headers["cache-control"] == "no-store"
            # Input rejection remains distinct even when the service is unavailable.
            invalid = await client.post(
                "/api/flight-contexts",
                json=artificial_intent(snapshot) | {"view": "replay"},
                headers=intent_headers(),
            )
            assert invalid.status_code == 409
    fresh = create_app(Settings(provider="synthetic"))
    async with AsyncClient(
        transport=ASGITransport(app=fresh), base_url="http://testserver"
    ) as client:
        for response in [
            await client.get("/api/flight-contexts"),
            await client.post(
                "/api/flight-contexts",
                json=artificial_intent(snapshot),
                headers=intent_headers(),
            ),
        ]:
            assert response.status_code == 503
            assert response.json()["reason"] == "lifecycle_unavailable"
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_context_duplicate_and_missing_protective_headers(context_api):
    import json

    from test_airlabs_models import artificial_intent

    app, service, snapshot = context_api
    good = list(intent_headers().items())
    variants = [
        [(k, v) for k, v in good if k != name]
        for name in ("Origin", "Content-Type", "X-Skytrace-Flight-Intent")
    ] + [good + [(name, value)] for name, value in good[:3]]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for headers in variants:
            response = await client.post(
                "/api/flight-contexts",
                content=json.dumps(artificial_intent(snapshot)),
                headers=headers,
            )
            assert response.status_code == 403
    assert service.submissions == 0


@pytest.mark.anyio
async def test_context_streamed_size_limit_and_exact_boundary(context_api):
    import json

    from test_airlabs_models import artificial_intent

    app, service, snapshot = context_api
    raw = json.dumps(artificial_intent(snapshot)).encode()
    exact = raw + b" " * (2048 - len(raw))

    async def oversized():
        yield exact
        yield b" "

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        accepted = await client.post(
            "/api/flight-contexts", content=exact, headers=intent_headers()
        )
        rejected = await client.post(
            "/api/flight-contexts",
            content=oversized(),
            headers=intent_headers(),
        )
    assert accepted.status_code == 202
    assert rejected.status_code == 422
    assert rejected.json()["reason"] == "body_too_large"
    assert service.submissions == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "retry,expected",
    [
        (1.2, "2"),
        (0, "0"),
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
        (10**400, None),
        (None, None),
    ],
)
async def test_context_rejection_preserves_only_safe_retry(context_api, retry, expected):
    from test_airlabs_models import artificial_intent

    from skytrace.services.airlabs_context import SubmissionRejected

    app, service, snapshot = context_api

    def reject(*args):
        raise SubmissionRejected(503, "rate_limited", retry)

    service.submit = reject
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/flight-contexts", json=artificial_intent(snapshot), headers=intent_headers()
        )
    assert response.status_code == 503
    assert response.headers.get("retry-after") == expected
    assert response.json()["retry_in_seconds"] == (retry if expected is not None else None)


@pytest.mark.anyio
@pytest.mark.parametrize("transition", ["match", "aba", "observer_failure"])
async def test_real_lifespan_active_owner_cohort_admission_coalescing_and_cleanup(
    monkeypatch, tmp_path, transition
):
    import asyncio
    import json

    import httpx
    from test_airlabs_client import artificial_response
    from test_airlabs_context import World
    from test_airlabs_quota import Clock, artificial_seed, contents

    import skytrace.main as main
    from skytrace.services.airlabs_client import AirLabsClient
    from skytrace.services.airlabs_quota import AirLabsQuota, seed_ledger

    clock = Clock()
    world = World(clock, count=400)
    ledger = tmp_path / "artificial.sqlite"
    seed_ledger(ledger, artificial_seed(), now=clock.now)
    calls = []
    release = asyncio.Event()
    clients = []
    quotas = []
    offered = []
    monkeypatch.setattr(main.HistoryStore, "offer", lambda self, batch: offered.append(batch))

    async def transport(request):
        calls.append(request)
        # Accounting must already be durable before mock dispatch.
        assert len(contents(ledger)[1]) == 1
        await release.wait()
        return httpx.Response(
            200,
            json=artificial_response(
                hex="000000",
                flight_icao="ZZZ000",
                updated=int(clock.now.timestamp()),
                dep_time_ts=int(clock.now.timestamp()) - 3600,
                arr_time_ts=int(clock.now.timestamp()) + 3600,
            ),
        )

    def client_factory(key):
        client = AirLabsClient(
            key, transport=httpx.MockTransport(transport), clock=lambda: clock.now
        )
        clients.append(client)
        return client

    def quota_factory(path):
        quota = AirLabsQuota(path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
        quotas.append(quota)
        return quota

    monkeypatch.setattr(main, "AirLabsClient", client_factory)
    monkeypatch.setattr(main, "AirLabsQuota", quota_factory)
    original_owner = main.ActiveContexts

    class ClockOwner(original_owner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, clock=lambda: clock.now, monotonic=lambda: clock.mono)

    monkeypatch.setattr(main, "ActiveContexts", ClockOwner)
    # Publish artificial snapshots through the real runtime observer, without a provider.
    monkeypatch.setattr(main.IngestionService, "snapshot", lambda self: world.message.data)
    app = create_app(
        Settings(
            provider="synthetic",
            airlabs_mode="selected",
            airlabs_api_key="artificial-key",
            airlabs_ledger_path=ledger,
        ),
        airlabs_activation_approved=True,
    )
    async with app.router.lifespan_context(app):
        owner = app.state.flight_contexts
        assert isinstance(owner, ClockOwner)
        assert owner._snapshot.__self__ is app.state.runtime
        app.state.runtime.ingestion.clock = lambda: clock.now
        clock.advance(60)
        world.refresh()
        snapshot = app.state.runtime.snapshot()
        intent = world.intent().model_copy(
            update={"stream_id": snapshot.stream_id, "sequence": max(0, snapshot.sequence - 1)}
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for _ in range(12):
                batch = (await client.get("/api/flight-contexts")).json()
                assert batch["tracked_aircraft"] == len(batch["contexts"]) == 400
            assert not calls and not contents(ledger)[1]
            for changes, headers in [
                ({"view": "replay"}, intent_headers()),
                ({"callsign": "ZZZ999"}, intent_headers()),
                ({}, intent_headers() | {"Origin": "https://foreign.example"}),
            ]:
                response = await client.post(
                    "/api/flight-contexts",
                    json=intent.model_dump(mode="json") | changes,
                    headers=headers,
                )
                assert response.status_code in {403, 409}
            assert not calls and not contents(ledger)[1]
            first = await client.post(
                "/api/flight-contexts",
                json=intent.model_dump(mode="json"),
                headers=intent_headers(),
            )
            assert first.status_code == 202
            async with asyncio.timeout(1):
                while not calls:
                    await asyncio.sleep(0)
            second = await client.post(
                "/api/flight-contexts",
                json=intent.model_dump(mode="json"),
                headers=intent_headers(),
            )
            assert second.status_code == 202 and second.json()["state"] == "pending"
            runtime = app.state.runtime
            assert runtime.subscriber_count == 0
            if transition == "aba":
                world.change(callsign="ZZZ999")
                runtime.snapshot()
                world.change(callsign="ZZZ000")
                runtime.snapshot()
            elif transition == "observer_failure":

                def broken(message):
                    raise RuntimeError("artificial private observer failure")

                runtime.airlabs_snapshot_observer = broken
                world.change(callsign="ZZZ999")
                runtime.snapshot()
                assert not owner._slots and not owner._running
                assert runtime.poller_status == "running"
            release.set()
            async with asyncio.timeout(1):
                while owner._inflight is not None:
                    await asyncio.sleep(0)
            result = (await client.get("/api/flight-contexts")).json()
            expected = 1 if transition == "match" else 0
            assert result["counts"]["matched"] == result["two_endpoint_contexts"] == expected
            assert result["confirmed_current_flights"] == 0
            assert len(calls) == len(contents(ledger)[1]) == 1
            assert all("context" not in plane.model_dump() for plane in snapshot.data.aircraft)
            # Polling and history delivery continue even when optional context fails.
            async with asyncio.timeout(1):
                while not offered:
                    await asyncio.sleep(0)
            from skytrace.models import ProviderBatch

            assert all(isinstance(batch, ProviderBatch) for batch in offered)
            assert all(batch.source.name == "synthetic" for batch in offered)
            assert all(
                "airlabs" not in json.dumps(batch.model_dump(mode="json")).lower()
                for batch in offered
            )
            telemetry = (await client.get("/api/aircraft")).json()
            assert len(telemetry["data"]["aircraft"]) == 400
            assert "context" not in telemetry["data"]["aircraft"][0]
    assert clients[0]._client.is_closed
    assert quotas[0]._fd is None
    reopened = AirLabsQuota(ledger, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    reopened.start()
    reopened.close()
