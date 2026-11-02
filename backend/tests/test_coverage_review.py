"""Independent regression checks for the sampled-hub coverage package."""

from datetime import UTC, datetime, timedelta

import pytest

from skytrace.config import Settings
from skytrace.models import ProviderBatch, TrackingArea
from skytrace.providers.base import AircraftProvider, ProviderError
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.cache import AircraftCache
from skytrace.services.ingestion import IngestionService

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now_value = NOW
        self.monotonic_value = 1000.0

    def now(self) -> datetime:
        return self.now_value

    def monotonic(self) -> float:
        return self.monotonic_value

    def advance(self, seconds: float) -> None:
        self.now_value += timedelta(seconds=seconds)
        self.monotonic_value += seconds


@pytest.mark.anyio
async def test_failed_scope_yields_next_shared_slot_after_backoff() -> None:
    clock = Clock()
    batch = await SyntheticProvider(clock=clock.now).fetch(TrackingArea())

    class Provider(AircraftProvider):
        source = "synthetic"

        def __init__(self) -> None:
            self.areas: list[TrackingArea] = []
            self.results: list[ProviderBatch | ProviderError] = [
                ProviderError("temporary"),
                batch,
                batch,
            ]

        async def fetch(self, area: TrackingArea) -> ProviderBatch:
            self.areas.append(area)
            result = self.results.pop(0)
            if isinstance(result, ProviderError):
                raise result
            return result

    provider = Provider()
    settings = Settings(
        coverage_preset="world_hubs_v1",
        stale_after_seconds=420,
        expire_after_seconds=900,
    )
    ingest = IngestionService(
        provider,
        settings,
        clock=clock.now,
        monotonic=clock.monotonic,
        jitter=lambda _lower, upper: upper,
    )

    assert await ingest.poll_once()
    assert ingest.snapshot().retry_in_seconds == 36
    clock.advance(36)
    assert await ingest.poll_once()
    clock.advance(30)
    assert await ingest.poll_once()

    assert [(area.latitude_deg, area.longitude_deg) for area in provider.areas] == [
        (39.8561, -104.6737),
        (51.47, -0.4543),
        (35.5494, 139.7798),
    ]
    coverage = {scope.id: scope for scope in ingest.snapshot().coverage}
    assert coverage["den"].status == "degraded"
    assert coverage["lhr"].status == "healthy"
    assert ingest.snapshot().active_scope_id == "syd"


@pytest.mark.anyio
async def test_replacing_one_overlap_membership_keeps_the_other_scope_record() -> None:
    batch = await SyntheticProvider(clock=lambda: NOW).fetch(TrackingArea())
    shared = batch.aircraft[0]
    alpha = batch.model_copy(update={"aircraft": (shared,), "raw_record_count": 1})
    bravo = batch.model_copy(
        update={
            "aircraft": (shared.model_copy(update={"callsign": "BRAVO1"}),),
            "raw_record_count": 1,
        }
    )
    empty = batch.model_copy(update={"aircraft": (), "raw_record_count": 0})
    cache = AircraftCache(max_aircraft=10, retention_seconds=300)

    cache.replace_scope("alpha", alpha, NOW)
    cache.replace_scope("bravo", bravo, NOW)
    cache.replace_scope("alpha", empty, NOW)

    remaining = cache.states(NOW)
    assert len(remaining) == 1
    assert remaining[0].callsign == "BRAVO1"
def test_custom_single_area_is_not_mislabelled_denver():
    from skytrace.models import TrackingArea
    from skytrace.services.coverage import coverage_plan

    plan = coverage_plan("den", default_area=TrackingArea(latitude_deg=0, longitude_deg=0))
    assert plan.areas[0].identifier == "configured"
    assert plan.areas[0].name == "Configured area"

