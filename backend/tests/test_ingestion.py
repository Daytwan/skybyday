import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from skytrace.config import Settings
from skytrace.models import ProviderBatch, TrackingArea
from skytrace.providers.base import AircraftProvider, ProviderError
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.cache import AircraftCache
from skytrace.services.ingestion import IngestionService

NOW = datetime(2026, 10, 18, 18, tzinfo=UTC)


@dataclass
class Clock:
    instant: datetime = NOW
    ticks: float = 1000

    def now(self) -> datetime:
        return self.instant

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)
        self.ticks += seconds


class ScriptedProvider(AircraftProvider):
    source = "synthetic"

    def __init__(self, results: list[ProviderBatch | ProviderError]) -> None:
        self.results = results
        self.calls = 0

    async def fetch(self, area: TrackingArea) -> ProviderBatch:
        self.calls += 1
        await asyncio.sleep(0)
        result = self.results.pop(0)
        if isinstance(result, ProviderError):
            raise result
        return result


def service(provider: AircraftProvider, clock: Clock) -> IngestionService:
    return IngestionService(
        provider,
        Settings(),
        clock=clock.now,
        monotonic=clock.monotonic,
        jitter=lambda lower, upper: upper,
    )


@pytest.mark.anyio
async def test_synthetic_provider_is_deterministic_and_uses_configured_area() -> None:
    clock = Clock()
    provider = SyntheticProvider(clock=clock.now)
    area = TrackingArea(latitude_deg=0, longitude_deg=0, radius_nm=1)
    first = await provider.fetch(area)
    assert first == await provider.fetch(area)
    assert first.source.name == "synthetic"
    assert first.source.license == "MIT"
    assert first.http is None
    assert len(first.aircraft) == 2
    assert all(
        abs(plane.latitude_deg) < 0.01 and abs(plane.longitude_deg) < 0.01
        for plane in first.aircraft
    )
    assert first.aircraft[0].latitude_deg != first.aircraft[1].latitude_deg
    assert all(plane.source_type == "synthetic" for plane in first.aircraft)
    assert 89 < first.aircraft[0].ground_track_deg < 91
    assert 269 < first.aircraft[1].ground_track_deg < 271
    assert all(
        plane.true_heading_deg is None and plane.pitch_deg is None and plane.roll_deg is None
        for plane in first.aircraft
    )
    clock.advance(30)
    later = await provider.fetch(area)
    assert later.aircraft[0].longitude_deg != first.aircraft[0].longitude_deg
    assert later.aircraft[0].ground_track_deg != first.aircraft[0].ground_track_deg


@pytest.mark.anyio
async def test_shared_calls_coalesce_and_readers_do_not_fetch() -> None:
    clock = Clock()
    batch = await SyntheticProvider(clock=clock.now).fetch(TrackingArea())
    provider = ScriptedProvider([batch])
    ingest = service(provider, clock)
    assert sorted(await asyncio.gather(ingest.poll_once(), ingest.poll_once())) == [False, True]
    for _ in range(5):
        assert len(ingest.snapshot().aircraft) == 2
    assert provider.calls == 1
    assert ingest.snapshot().retry_in_seconds == 30


@pytest.mark.anyio
async def test_retry_after_exceeds_local_cap_and_recovery_resets_backoff() -> None:
    clock = Clock()
    batch = await SyntheticProvider(clock=clock.now).fetch(TrackingArea())
    provider = ScriptedProvider(
        [
            ProviderError("HTTP 429", retry_after_seconds=1800),
            ProviderError("HTTP 503"),
            batch,
        ]
    )
    ingest = service(provider, clock)
    await ingest.poll_once()
    assert ingest.snapshot().retry_in_seconds == 1800
    assert ingest.snapshot().status == "degraded"
    clock.advance(1799)
    assert await ingest.poll_once() is False
    assert provider.calls == 1
    clock.advance(1)
    await ingest.poll_once()
    assert ingest.snapshot().retry_in_seconds == 72
    clock.advance(72)
    await ingest.poll_once()
    assert ingest.snapshot().status == "healthy"
    assert ingest.snapshot().consecutive_failures == 0
    assert ingest.snapshot().retry_in_seconds == 30


@pytest.mark.anyio
async def test_backoff_is_bounded_and_access_errors_suspend_requests() -> None:
    clock = Clock()
    provider = ScriptedProvider(
        [ProviderError("unavailable") for _ in range(12)]
        + [ProviderError("HTTP 403", retryable=False)]
    )
    ingest = service(provider, clock)
    delays = []
    for _ in range(12):
        await ingest.poll_once()
        delay = ingest.snapshot().retry_in_seconds
        assert 30 <= delay <= 900
        delays.append(delay)
        clock.advance(delay)
    assert delays[:3] == [36, 72, 144]
    assert delays[-1] == 900
    await ingest.poll_once()
    assert ingest.snapshot().status == "suspended"
    assert ingest.snapshot().retry_in_seconds is None
    clock.advance(86400)
    assert await ingest.poll_once() is False
    assert provider.calls == 13


@pytest.mark.anyio
async def test_outage_keeps_then_expires_observations_using_position_time() -> None:
    clock = Clock()
    provider = SyntheticProvider(clock=clock.now)
    batch = await provider.fetch(TrackingArea())
    scripted = ScriptedProvider([batch, ProviderError("HTTP 429", retry_after_seconds=1800)])
    ingest = service(scripted, clock)
    await ingest.poll_once()
    original_time = ingest.snapshot().aircraft[0].position_observed_at
    clock.advance(30)
    await ingest.poll_once()
    assert ingest.snapshot().aircraft[0].position_observed_at == original_time
    clock.advance(61)
    assert ingest.snapshot().stale_aircraft_count == 2
    clock.advance(210)
    assert ingest.snapshot().aircraft == ()
    assert scripted.calls == 2
    assert ingest.snapshot().last_success_at == NOW


@pytest.mark.anyio
async def test_cache_bounds_duplicates_regressions_and_empty_snapshots() -> None:
    clock = Clock()
    batch = await SyntheticProvider(clock=clock.now).fetch(TrackingArea())
    cache = AircraftCache(max_aircraft=1, retention_seconds=300)
    cache.replace(batch, NOW)
    assert len(cache.states(NOW)) == 1
    old = cache.states(NOW)[0]
    regressed = old.model_copy(update={"position_observed_at": NOW - timedelta(seconds=10)})
    cache.replace(batch.model_copy(update={"aircraft": (regressed, regressed)}), NOW)
    assert cache.states(NOW) == (old,)
    no_position = old.model_copy(
        update={
            "latitude_deg": None,
            "longitude_deg": None,
            "position_observed_at": None,
        }
    )
    cache.replace(batch.model_copy(update={"aircraft": (no_position,), "raw_record_count": 1}), NOW)
    assert cache.states(NOW)[0].position_observed_at == old.position_observed_at
    empty = batch.model_copy(update={"aircraft": (), "raw_record_count": 0})
    cache.replace(empty, NOW)
    assert cache.states(NOW) == ()


@pytest.mark.anyio
async def test_scoped_cache_keeps_other_circle_and_removes_replaced_members() -> None:
    batch = await SyntheticProvider(clock=lambda: NOW).fetch(TrackingArea())
    first = batch.aircraft[0]
    second = batch.aircraft[1].model_copy(update={"icao24": "abcdef"})
    alpha = batch.model_copy(update={"aircraft": (first,), "raw_record_count": 1})
    bravo = batch.model_copy(update={"aircraft": (second,), "raw_record_count": 1})
    empty = batch.model_copy(update={"aircraft": (), "raw_record_count": 0})
    cache = AircraftCache(max_aircraft=10, retention_seconds=300)
    cache.replace_scope("alpha", alpha, NOW)
    cache.replace_scope("bravo", bravo, NOW)
    assert {plane.icao24 for plane in cache.states(NOW)} == {first.icao24, second.icao24}
    cache.replace_scope("alpha", empty, NOW)
    assert [plane.icao24 for plane in cache.states(NOW)] == [second.icao24]


@pytest.mark.anyio
async def test_scoped_cache_retains_newer_same_scope_observation_and_reports_capacity() -> None:
    batch = await SyntheticProvider(clock=lambda: NOW).fetch(TrackingArea())
    current = batch.aircraft[0]
    older = current.model_copy(update={"position_observed_at": NOW - timedelta(seconds=10)})
    older_batch = batch.model_copy(update={"aircraft": (older,), "raw_record_count": 1})
    other = batch.aircraft[1].model_copy(update={"icao24": "abcdef"})
    other_batch = batch.model_copy(update={"aircraft": (other,), "raw_record_count": 1})
    cache = AircraftCache(max_aircraft=1, retention_seconds=300)
    cache.replace_scope(
        "alpha", batch.model_copy(update={"aircraft": (current,), "raw_record_count": 1}), NOW
    )
    cache.replace_scope("alpha", older_batch, NOW)
    assert cache.states(NOW)[0].position_observed_at == current.position_observed_at
    cache.replace_scope("bravo", other_batch, NOW)
    collected, published, truncated, capacity = cache.collection_counts(NOW)
    assert (collected, published, truncated, capacity) == (2, 1, 1, 1)


@pytest.mark.anyio
async def test_world_hubs_rotate_one_shared_slot_and_publish_scope_freshness() -> None:
    clock = Clock()
    provider = SyntheticProvider(clock=clock.now)
    settings = Settings(
        coverage_preset="world_hubs_v1",
        stale_after_seconds=420,
        expire_after_seconds=900,
    )
    ingest = IngestionService(
        provider, settings, clock=clock.now, monotonic=clock.monotonic, jitter=lambda a, b: b
    )
    seen = []
    original_fetch = provider.fetch

    async def capture(area: TrackingArea) -> ProviderBatch:
        seen.append(area)
        return await original_fetch(area)

    provider.fetch = capture  # type: ignore[method-assign]
    for _ in range(6):
        assert await ingest.poll_once()
        clock.advance(30)
    assert [(area.latitude_deg, area.longitude_deg, area.radius_nm) for area in seen] == [
        (39.8561, -104.6737, 60),
        (51.47, -0.4543, 120),
        (35.5494, 139.7798, 120),
        (-33.9399, 151.1753, 120),
        (25.2532, 55.3657, 120),
        (-23.4356, -46.4731, 120),
    ]
    snapshot = ingest.snapshot()
    assert snapshot.active_scope_id == "den"
    assert len(snapshot.coverage) == 6
    assert all(scope.status == "healthy" and not scope.stale for scope in snapshot.coverage)
    assert {plane.icao24 for plane in snapshot.aircraft} == {"000001", "000002"}


@pytest.mark.anyio
async def test_cancellation_propagates_and_does_not_immediately_repoll() -> None:
    started = asyncio.Event()

    class WaitingProvider(AircraftProvider):
        source = "synthetic"

        async def fetch(self, area: TrackingArea) -> ProviderBatch:
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    clock = Clock()
    ingest = service(WaitingProvider(), clock)
    task = asyncio.create_task(ingest.poll_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ingest.poll_once() is False
    assert ingest.snapshot().consecutive_failures == 0
