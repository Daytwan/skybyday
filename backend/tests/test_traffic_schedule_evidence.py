"""One missing combination: a scoped Retry-After shares the global deadline."""

from datetime import UTC, datetime, timedelta

import pytest

from skytrace.config import Settings
from skytrace.models import TrackingArea
from skytrace.providers.base import ProviderError
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.ingestion import IngestionService


@pytest.mark.anyio
async def test_scoped_retry_after_blocks_all_scopes_until_shared_deadline():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ticks = 0.0
    calls = []

    class RecordingProvider(SyntheticProvider):
        async def fetch(self, area: TrackingArea):
            calls.append((ticks, area.latitude_deg))
            if len(calls) == 1:
                raise ProviderError("artificial HTTP 429", retry_after_seconds=1800)
            return await super().fetch(area)

    ingest = IngestionService(
        RecordingProvider(clock=lambda: now),
        Settings(
            coverage_preset="world_hubs_v1", stale_after_seconds=420, expire_after_seconds=900
        ),
        clock=lambda: now,
        monotonic=lambda: ticks,
        jitter=lambda _lower, upper: upper,
    )
    assert await ingest.poll_once()
    assert ingest.snapshot().retry_in_seconds == 1800
    assert ingest.snapshot().active_scope_id == "lhr"
    for advance in (30, 150, 1619):
        now += timedelta(seconds=advance)
        ticks += advance
        assert await ingest.poll_once() is False
        assert len(calls) == 1
    now += timedelta(seconds=1)
    ticks += 1
    assert await ingest.poll_once()
    assert calls == [(0.0, 39.8561), (1800.0, 51.47)]
    assert ingest.snapshot().retry_in_seconds == 30
    assert ingest.snapshot().active_scope_id == "hnd"
    assert await ingest.poll_once() is False
    assert len(calls) == 2
