import asyncio
from datetime import UTC, datetime

import pytest

from skytrace.config import Settings
from skytrace.history import MAX_BATCH_OBSERVATIONS, QUEUE_BATCHES, HistoryStore
from skytrace.models import AircraftObservation, ProviderBatch, SourceAttribution
from skytrace.services.ingestion import IngestionService
from skytrace.services.runtime import TelemetryRuntime

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
SOURCE = SourceAttribution(
    name="synthetic",
    attribution="Test source",
    url=None,
    license="MIT",
    license_url=None,
)


def observation(icao24: str = "000001", *, positioned: bool = True) -> AircraftObservation:
    position = (
        {"position_observed_at": NOW, "latitude_deg": 39.8, "longitude_deg": -104.7}
        if positioned
        else {}
    )
    return AircraftObservation(
        icao24=icao24,
        source="synthetic",
        source_type="test",
        provider_timestamp=NOW,
        received_at=NOW,
        **position,
    )


def batch(*planes: AircraftObservation) -> ProviderBatch:
    return ProviderBatch(
        source=SOURCE,
        provider_timestamp=NOW,
        received_at=NOW,
        raw_record_count=len(planes),
        rejected_record_count=0,
        aircraft=planes,
    )


def enabled_settings() -> Settings:
    return Settings(
        history_enabled=True,
        history_database_url="postgresql://user:secret@localhost/skytrace",
    )


def test_nonblocking_queue_is_bounded_and_excludes_null_positions() -> None:
    store = HistoryStore(enabled_settings())
    store._accepting = True
    assert store.offer(batch(observation(positioned=False))) is True
    assert store.status().accepted_batches == 0

    item = batch(observation())
    for _ in range(QUEUE_BATCHES):
        assert store.offer(item) is True
    assert store.offer(item) is False
    status = store.status()
    assert status.queued_batches == QUEUE_BATCHES
    assert status.dropped_queue_batches == 1
    assert status.dropped_queue_observations == 1
    assert status.queue_capacity_batches == 8
    assert MAX_BATCH_OBSERVATIONS == 10_000


@pytest.mark.anyio
async def test_runtime_offers_only_each_fresh_successful_batch_once() -> None:
    offered: list[ProviderBatch] = []
    sleeping = asyncio.Event()
    blocked = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeping.set()
        await blocked.wait()

    class Sink:
        def offer(self, value: ProviderBatch) -> bool:
            offered.append(value)
            return True

    class Provider:
        source = "synthetic"

        async def fetch(self, area):
            return batch(observation())

    settings = Settings()
    service = IngestionService(Provider(), settings)
    runtime = TelemetryRuntime(service, history=Sink(), sleep=sleep)
    runtime.start()
    try:
        await asyncio.wait_for(sleeping.wait(), 1)
        runtime.snapshot()
        runtime.snapshot()
        assert len(offered) == 1
    finally:
        await runtime.stop()


@pytest.mark.anyio
async def test_shutdown_is_bounded_and_counts_inflight_and_queued_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = asyncio.Event()

    class BlockingStore(HistoryStore):
        async def _insert(self, item):
            await blocked.wait()

    store = BlockingStore(enabled_settings())
    store.start()
    store.offer(batch(observation()))
    store.offer(batch(observation("000002")))
    async with asyncio.timeout(1):
        while store._inflight_observations == 0:
            await asyncio.sleep(0)
    monkeypatch.setattr("skytrace.history.SHUTDOWN_DRAIN_SECONDS", 0.01)
    await store.stop()
    assert store.status().dropped_shutdown_observations == 2


@pytest.mark.anyio
async def test_write_failures_have_finite_retries_and_observable_loss() -> None:
    delays: list[float] = []

    async def no_wait(seconds: float) -> None:
        delays.append(seconds)

    class FailingStore(HistoryStore):
        async def _insert(self, item):
            raise OSError("database offline")

    store = FailingStore(enabled_settings(), sleep=no_wait)
    store.start()
    assert store.offer(batch(observation())) is True
    await asyncio.wait_for(store._queue.join(), 1)
    status = store.status()
    assert delays == [5.0, 10.0, 20.0]
    assert status.retry_count == 3
    assert status.failure_count == 4
    assert status.dropped_write_observations == 1
    assert "secret" not in (status.last_error or "")
    await store.stop()
