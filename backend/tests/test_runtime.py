import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from skytrace.config import Settings
from skytrace.main import create_app
from skytrace.providers.adsblol import AdsbLolProvider
from skytrace.providers.base import ProviderError
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.ingestion import IngestionService
from skytrace.services.runtime import TelemetryRuntime


@pytest.mark.anyio
async def test_default_live_app_keeps_failure_visible_without_synthetic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def throttled(self, area):
        nonlocal calls
        calls += 1
        raise ProviderError("HTTP 429", retry_after_seconds=1800)

    monkeypatch.setattr(AdsbLolProvider, "fetch", throttled)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(1):
            while app.state.runtime.ingestion.snapshot().status == "idle":
                await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for _ in range(3):
                snapshot = (await client.get("/api/aircraft")).json()["data"]
                assert snapshot["source"] == "adsblol"
                assert snapshot["status"] == "degraded"
                assert snapshot["aircraft"] == []
                assert snapshot["retry_in_seconds"] > 1790
    assert calls == 1


@pytest.mark.anyio
async def test_one_lifespan_poller_shared_reads_and_closed_client() -> None:
    class CountingProvider(SyntheticProvider):
        calls = 0

        async def fetch(self, area):
            self.calls += 1
            return await super().fetch(area)

    provider = CountingProvider()
    app = create_app(provider=provider)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with asyncio.timeout(1):
            while provider.calls == 0:
                await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            responses = await asyncio.gather(*[client.get("/api/aircraft") for _ in range(10)])
        subscriptions = [runtime.subscribe() for _ in range(3)]
        assert provider.calls == 1
        assert all(len(response.json()["data"]["aircraft"]) == 2 for response in responses)
        assert all(
            json.loads(queue.get_nowait())["data"]["status"] == "healthy" for queue in subscriptions
        )
        assert not app.state.http_client.is_closed
    assert runtime.task.done()
    assert app.state.http_client.is_closed
    assert all(queue.get_nowait() is None for queue in subscriptions)
    assert runtime.subscriber_count == 0


@pytest.mark.anyio
async def test_lifespan_shutdown_cancels_an_inflight_provider() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingProvider(SyntheticProvider):
        async def fetch(self, area):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    app = create_app(provider=WaitingProvider())
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 1)
    assert cancelled.is_set()
    assert app.state.runtime.task.done()
    assert app.state.http_client.is_closed


@pytest.mark.anyio
async def test_loop_broadcasts_expiry_during_retry_after_then_recovers() -> None:
    now = datetime(2026, 10, 18, 18, tzinfo=UTC)
    ticks = 0.0
    steps = asyncio.Queue()
    sleeping = asyncio.Event()

    async def step_sleep(seconds):
        nonlocal now, ticks
        sleeping.set()
        delta = await steps.get()
        now += timedelta(seconds=delta)
        ticks += delta

    class FailingOnce(SyntheticProvider):
        calls = 0

        async def fetch(self, area):
            self.calls += 1
            if self.calls == 2:
                raise ProviderError("HTTP 429", retry_after_seconds=1800)
            return await super().fetch(area)

    provider = FailingOnce(clock=lambda: now)
    service = IngestionService(provider, Settings(), clock=lambda: now, monotonic=lambda: ticks)
    runtime = TelemetryRuntime(service, sleep=step_sleep)
    runtime.start()
    try:
        await asyncio.wait_for(sleeping.wait(), 1)
        queue = runtime.subscribe()
        initial = json.loads(queue.get_nowait())
        assert initial["data"]["status"] == "healthy"
        original_time = initial["data"]["aircraft"][0]["position_observed_at"]

        async def advance(seconds):
            sleeping.clear()
            steps.put_nowait(seconds)
            await asyncio.wait_for(sleeping.wait(), 1)

        await advance(30)
        failure = json.loads(queue.get_nowait())
        assert failure["data"]["status"] == "degraded"
        assert failure["data"]["retry_in_seconds"] == 1800
        assert failure["data"]["aircraft"][0]["position_observed_at"] == original_time
        await advance(61)
        assert json.loads(queue.get_nowait())["data"]["stale_aircraft_count"] == 2
        await advance(210)
        expired = json.loads(queue.get_nowait())
        assert expired["data"]["aircraft"] == []
        assert expired["data"]["status"] == "degraded"
        assert provider.calls == 2
        await advance(1529)
        recovered = json.loads(queue.get_nowait())
        assert recovered["data"]["status"] == "healthy"
        assert len(recovered["data"]["aircraft"]) == 2
        assert recovered["sequence"] > expired["sequence"] > initial["sequence"]
        assert provider.calls == 3
    finally:
        await runtime.stop()


@pytest.mark.anyio
async def test_latest_only_queue_capacity_reconnect_and_no_countdown_broadcasts() -> None:
    now = datetime(2026, 10, 18, 18, tzinfo=UTC)
    provider = SyntheticProvider(clock=lambda: now)
    service = IngestionService(provider, Settings(ws_max_clients=1), clock=lambda: now)
    runtime = TelemetryRuntime(service)
    queue = runtime.subscribe()
    first = json.loads(queue.get_nowait())
    assert runtime.subscribe() is None
    await service.poll_once()
    runtime.snapshot()
    for age in [100, 200, 301]:
        now = datetime(2026, 10, 18, 18, tzinfo=UTC) + timedelta(seconds=age)
        runtime.snapshot()
        assert queue.qsize() == 1
    latest = json.loads(queue.get_nowait())
    assert latest["data"]["aircraft"] == []
    assert latest["sequence"] > first["sequence"]
    runtime.snapshot()
    assert queue.empty()
    runtime.unsubscribe(queue)
    reconnected = runtime.subscribe()
    assert json.loads(reconnected.get_nowait())["sequence"] == latest["sequence"]
    other = TelemetryRuntime(service)
    assert other.stream_id != runtime.stream_id
    await runtime.stop()
    assert reconnected.get_nowait() is None


@pytest.mark.anyio
async def test_scope_freshness_age_does_not_create_per_second_websocket_snapshots() -> None:
    now = datetime(2026, 10, 18, 18, tzinfo=UTC)
    settings = Settings(
        coverage_preset="world_hubs_v1",
        stale_after_seconds=420,
        expire_after_seconds=900,
    )
    service = IngestionService(SyntheticProvider(clock=lambda: now), settings, clock=lambda: now)
    runtime = TelemetryRuntime(service)
    await service.poll_once()
    queue = runtime.subscribe()
    assert queue is not None
    _ = queue.get_nowait()
    now += timedelta(seconds=1)
    runtime.snapshot()
    assert queue.empty()
    await runtime.stop()
    assert runtime.subscribe() is None


@pytest.mark.anyio
async def test_programming_failure_is_visible_and_not_retried(caplog) -> None:
    class BrokenProvider(SyntheticProvider):
        calls = 0

        async def fetch(self, area):
            self.calls += 1
            raise RuntimeError("bug")

    provider = BrokenProvider()
    runtime = TelemetryRuntime(IngestionService(provider, Settings()))
    runtime.start()
    await asyncio.wait_for(runtime.task, 1)
    assert runtime.snapshot().poller_status == "failed"
    assert provider.calls == 1
    assert "Telemetry poller stopped unexpectedly" in caplog.text
    await runtime.stop()


@pytest.mark.anyio
async def test_empty_success_and_outage_have_distinct_api_states() -> None:
    class EmptyProvider(SyntheticProvider):
        async def fetch(self, area):
            batch = await super().fetch(area)
            return batch.model_copy(update={"aircraft": (), "raw_record_count": 0})

    class UnavailableProvider(SyntheticProvider):
        async def fetch(self, area):
            raise ProviderError("offline", retry_after_seconds=1800)

    for provider, expected in [(EmptyProvider(), "healthy"), (UnavailableProvider(), "degraded")]:
        app = create_app(provider=provider)
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(1):
                while app.state.runtime.ingestion.snapshot().status == "idle":
                    await asyncio.sleep(0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                message = (await client.get("/api/aircraft")).json()
                assert message["data"]["aircraft"] == []
                assert message["data"]["status"] == expected


@pytest.mark.anyio
async def test_airlabs_observer_sees_changed_transitions_before_publish_without_subscribers():
    from test_airlabs_models import NOW, artificial_snapshot

    initial = artificial_snapshot()
    current = initial.data
    service = IngestionService(SyntheticProvider(clock=lambda: NOW), Settings(), clock=lambda: NOW)
    service.snapshot = lambda: current
    events = []
    queue = None

    def observer(message):
        if queue is not None:
            assert queue.empty()  # Observation precedes this changed publication.
        events.append((message.sequence, message.data.aircraft[0].callsign))

    runtime = TelemetryRuntime(service, airlabs_snapshot_observer=observer)
    runtime.snapshot()  # A, unsubscribed.
    runtime.snapshot()  # Identical projection, no rescan.
    queue = runtime.subscribe()
    queue.get_nowait()
    current = current.model_copy(
        update={
            "aircraft": (current.aircraft[0].model_copy(update={"callsign": "ZZZ002"}),),
        }
    )
    runtime.snapshot()  # B, subscribed.
    assert json.loads(queue.get_nowait())["data"]["aircraft"][0]["callsign"] == "ZZZ002"
    runtime.unsubscribe(queue)
    queue = None
    current = initial.data
    runtime.snapshot()  # A again, unsubscribed; the B transition was observed.
    runtime.snapshot()
    assert [callsign for _, callsign in events] == ["ZZZ001", "ZZZ002", "ZZZ001"]
    assert [sequence for sequence, _ in events] == [1, 2, 3]
    await runtime.stop()


@pytest.mark.anyio
async def test_observer_failure_is_safe_and_preserves_history_and_telemetry(caplog):
    from skytrace.models import ProviderBatch

    offered = []
    sleeping = asyncio.Event()

    class ArtificialHistory:
        def offer(self, batch):
            offered.append(batch)

    def broken_observer(message):
        raise RuntimeError("do not expose artificial secret text")

    async def pause(seconds):
        sleeping.set()
        await asyncio.Future()

    runtime = TelemetryRuntime(
        IngestionService(SyntheticProvider(), Settings(provider="synthetic")),
        history=ArtificialHistory(),
        sleep=pause,
        airlabs_snapshot_observer=broken_observer,
    )
    runtime.start()
    try:
        await asyncio.wait_for(sleeping.wait(), 1)
        snapshot = runtime.snapshot()
        assert snapshot.data.status == "healthy"
        assert len(snapshot.data.aircraft) == 2
        assert len(offered) == 1 and isinstance(offered[0], ProviderBatch)
        assert offered[0].source.name == "synthetic"
        assert "AirLabs snapshot observer failed" in caplog.text
        assert "artificial secret" not in caplog.text
    finally:
        await runtime.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["off", "selected"])
async def test_inert_airlabs_lifespan_creates_no_client_task_ledger_or_activation(
    monkeypatch, tmp_path, mode
):
    import sqlite3

    from skytrace.services.airlabs_models import AirLabsFlightContextService

    created_clients = []
    original_client = httpx.AsyncClient
    original_sqlite = sqlite3.connect
    opened_sqlite = []

    def track_client(*args, **kwargs):
        created_clients.append(original_client(*args, **kwargs))
        return created_clients[-1]

    def forbidden_sqlite(path, *args, **kwargs):
        # Existing bundled VRS read remains; any new database access is forbidden.
        assert str(path).endswith("/assets/vrs-standing-routes.sqlite3?mode=ro&immutable=1")
        opened_sqlite.append(str(path))
        return original_sqlite(path, *args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", track_client)
    monkeypatch.setattr(sqlite3, "connect", forbidden_sqlite)
    ledger = tmp_path / "never-created" / "ledger.sqlite"
    app = create_app(
        Settings(
            provider="synthetic",
            airlabs_mode=mode,
            airlabs_api_key="artificial-placeholder",
            airlabs_ledger_path=ledger,
        )
    )
    async with app.router.lifespan_context(app):
        assert len(created_clients) == 2  # Existing telemetry and metadata clients only.
        assert len(opened_sqlite) == 1
        assert type(app.state.flight_contexts) is AirLabsFlightContextService
        assert not any("airlabs" in task.get_name().lower() for task in asyncio.all_tasks())
        assert not ledger.parent.exists()
        assert app.state.flight_contexts.mode == mode
    assert all(client.is_closed for client in created_clients)
    assert app.state.runtime.task.done()
    assert not ledger.parent.exists()


@pytest.mark.anyio
async def test_explicit_injected_service_lifecycle_is_owned_once():
    from skytrace.services.airlabs_models import AirLabsFlightContextService

    class ArtificialService(AirLabsFlightContextService):
        starts = 0
        stops = 0
        observations = 0

        def start(self):
            self.starts += 1

        async def stop(self):
            self.stops += 1

        def observe_snapshot(self, snapshot):
            self.observations += 1

    contexts = ArtificialService()
    app = create_app(Settings(provider="synthetic"), airlabs_service=contexts)
    async with app.router.lifespan_context(app):
        app.state.runtime.snapshot()
        assert app.state.flight_contexts is contexts
        assert contexts.starts == 1
        assert contexts.observations >= 1
    assert contexts.stops == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure_callback_raises", [False, True])
async def test_observer_failure_callback_precedes_publication_and_cannot_break_delivery(
    caplog, failure_callback_raises
):
    invalidated = []
    offered = []
    reached_sleep = asyncio.Event()

    class History:
        def offer(self, batch):
            offered.append(batch)

    def observer(message):
        raise RuntimeError("artificial private exception")

    def failed():
        invalidated.append(True)
        if failure_callback_raises:
            raise RuntimeError("artificial private callback exception")

    async def pause(seconds):
        reached_sleep.set()
        await asyncio.Future()

    runtime = TelemetryRuntime(
        IngestionService(SyntheticProvider(), Settings(provider="synthetic")),
        history=History(),
        sleep=pause,
        airlabs_snapshot_observer=observer,
        airlabs_observer_failure=failed,
    )
    queue = runtime.subscribe()
    assert invalidated
    queue.get_nowait()
    runtime.start()
    try:
        await asyncio.wait_for(reached_sleep.wait(), 1)
        assert len(invalidated) >= 2
        message = json.loads(queue.get_nowait())
        assert message["data"]["status"] == "healthy"
        assert len(offered) == 1
        assert "private" not in caplog.text
    finally:
        await runtime.stop()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fault",
    ["history_start", "runtime_start", "runtime_stop", "history_stop", "owner_start", "owner_stop"],
)
async def test_lifespan_cleanup_survives_each_component_failure(monkeypatch, fault):
    import skytrace.main as main
    from skytrace.services.airlabs_models import AirLabsFlightContextService

    events = []

    class Owner(AirLabsFlightContextService):
        def start(self):
            events.append("owner_start")
            if fault == "owner_start":
                raise RuntimeError("private start failure")

        async def stop(self):
            events.append("owner_stop")
            if fault == "owner_stop":
                raise RuntimeError("private stop failure")

    original_runtime_stop = main.TelemetryRuntime.stop
    original_history_stop = main.HistoryStore.stop

    def history_start(self):
        events.append("history_start")
        if fault == "history_start":
            raise RuntimeError("history start")

    def runtime_start(self):
        events.append("runtime_start")
        if fault == "runtime_start":
            raise RuntimeError("runtime start")

    async def runtime_stop(self):
        events.append("runtime_stop")
        await original_runtime_stop(self)
        if fault == "runtime_stop":
            raise RuntimeError("runtime stop")

    async def history_stop(self):
        events.append("history_stop")
        await original_history_stop(self)
        if fault == "history_stop":
            raise RuntimeError("history stop")

    monkeypatch.setattr(main.HistoryStore, "start", history_start)
    monkeypatch.setattr(main.HistoryStore, "stop", history_stop)
    monkeypatch.setattr(main.TelemetryRuntime, "start", runtime_start)
    monkeypatch.setattr(main.TelemetryRuntime, "stop", runtime_stop)
    owner = Owner("selected")
    app = create_app(Settings(provider="synthetic", airlabs_mode="selected"), airlabs_service=owner)
    try:
        async with app.router.lifespan_context(app):
            if fault == "owner_start":
                assert app.state.flight_contexts is not owner
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    result = await client.get("/api/flight-contexts")
                    assert result.status_code == 200
                    assert result.json()["service_state"] == "unavailable"
    except RuntimeError:
        assert fault != "owner_start"
    else:
        assert fault == "owner_start"
    assert events[-3:] == ["runtime_stop", "owner_stop", "history_stop"]
    assert app.state.http_client.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid", ["missing", "corrupt", "unsafe", "served", "nonlocal", "secret", "client"]
)
async def test_approved_active_start_fails_closed_before_dispatch(monkeypatch, tmp_path, invalid):
    from pathlib import Path

    from test_airlabs_quota import Clock, artificial_seed

    import skytrace.main as main
    from skytrace.services.airlabs_models import AirLabsFlightContextService
    from skytrace.services.airlabs_quota import AirLabsQuota, seed_ledger

    clock = Clock()
    ledger = tmp_path / "artificial.sqlite"
    seed_ledger(ledger, artificial_seed(), now=clock.now)
    calls = []

    def client_factory(key):
        calls.append("client")
        raise RuntimeError("private client construction failure")

    monkeypatch.setattr(main, "AirLabsClient", client_factory)
    if invalid == "missing":
        ledger.unlink()
    elif invalid == "corrupt":
        ledger.write_bytes(b"artificial corrupt accounting")
    elif invalid == "unsafe":
        ledger.chmod(0o644)
    elif invalid == "served":
        ledger = Path(main.__file__).resolve()
    elif invalid == "nonlocal":
        original_read = Path.read_text

        def mountinfo(path, *args, **kwargs):
            if str(path) == "/proc/self/mountinfo":
                return "1 0 0:0 / / rw - nfs server:/ remote\n"
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", mountinfo)
    app = create_app(
        Settings(
            provider="synthetic",
            airlabs_mode="selected",
            airlabs_api_key=None if invalid == "secret" else "artificial",
            airlabs_ledger_path=ledger,
        ),
        airlabs_activation_approved=True,
    )
    async with app.router.lifespan_context(app):
        assert type(app.state.flight_contexts) is AirLabsFlightContextService
        async with asyncio.timeout(1):
            while app.state.runtime.ingestion.snapshot().status == "idle":
                await asyncio.sleep(0)
        assert app.state.runtime.snapshot().data.status == "healthy"
        assert not any(
            task.get_name() == "airlabs-selected-context" for task in asyncio.all_tasks()
        )
    assert calls == (["client"] if invalid == "client" else [])
    if invalid == "client":
        # Client construction failure must release the durable owner's lock.
        quota = AirLabsQuota(ledger, clock=lambda: clock.now, monotonic=lambda: clock.mono)
        quota.start()
        quota.close()


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["runtime_start", "runtime_stop", "client_close"])
async def test_active_owner_releases_ledger_and_client_on_lifespan_failure(
    monkeypatch, tmp_path, fault
):
    from pydantic import SecretStr
    from test_airlabs_quota import Clock, artificial_seed

    import skytrace.main as main
    from skytrace.services.airlabs_client import AirLabsClient
    from skytrace.services.airlabs_context import AirLabsFlightContextService
    from skytrace.services.airlabs_quota import AirLabsQuota, seed_ledger

    clock = Clock()
    ledger = tmp_path / "artificial.sqlite"
    seed_ledger(ledger, artificial_seed(), now=clock.now)
    client = AirLabsClient(
        SecretStr("artificial"),
        transport=httpx.MockTransport(lambda request: pytest.fail("No dispatch authorized")),
    )
    quota = AirLabsQuota(ledger, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    owner = AirLabsFlightContextService(
        "selected",
        snapshot=lambda: app.state.runtime.snapshot(),
        client_factory=lambda: client,
        quota_factory=lambda: quota,
        clock=lambda: clock.now,
        monotonic=lambda: clock.mono,
    )
    original_stop = main.TelemetryRuntime.stop
    original_close = client.close

    if fault == "runtime_start":

        def fail_start(self):
            raise RuntimeError("artificial runtime start failure")

        monkeypatch.setattr(main.TelemetryRuntime, "start", fail_start)
    elif fault == "runtime_stop":

        async def fail_stop(self):
            await original_stop(self)
            raise RuntimeError("artificial runtime stop failure")

        monkeypatch.setattr(main.TelemetryRuntime, "stop", fail_stop)
    else:

        async def fail_close():
            await original_close()
            raise RuntimeError("artificial client close failure")

        monkeypatch.setattr(client, "close", fail_close)
    app = create_app(Settings(provider="synthetic"), airlabs_service=owner)
    try:
        async with app.router.lifespan_context(app):
            assert quota._fd is not None
            assert owner._worker is not None
    except RuntimeError:
        assert fault != "client_close"
    else:
        assert fault == "client_close"
    assert client._client.is_closed
    assert quota._fd is None and owner._worker is None
    assert app.state.http_client.is_closed
    reopened = AirLabsQuota(ledger, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    reopened.start()
    reopened.close()
