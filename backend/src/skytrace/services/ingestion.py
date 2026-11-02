"""Single-owner, deadline-gated ingestion shared by the CLI and lifespan loop."""

import asyncio
import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime

from skytrace.config import Settings
from skytrace.models import (
    AircraftObservation,
    BatchInfo,
    FrozenModel,
    ProviderBatch,
    SourceName,
    TrackingArea,
)
from skytrace.providers.base import AircraftProvider, ProviderError, utc_now
from skytrace.services.cache import AircraftCache
from skytrace.services.coverage import CoveragePlan, coverage_plan

ProviderStatus = Literal["idle", "healthy", "degraded", "suspended"]
CoverageAreaStatus = Literal["idle", "healthy", "degraded"]


class CoverageAreaSnapshot(FrozenModel):
    id: str
    name: str
    area: TrackingArea
    status: CoverageAreaStatus
    last_success_at: AwareDatetime | None
    last_error: str | None
    freshness_age_seconds: float | None
    stale: bool


class IngestionSnapshot(FrozenModel):
    emitted_at: AwareDatetime
    source: SourceName
    status: ProviderStatus
    last_error: str | None
    consecutive_failures: int
    last_success_at: AwareDatetime | None
    retry_in_seconds: float | None
    batch: BatchInfo | None
    aircraft: tuple[AircraftObservation, ...]
    stale_aircraft_count: int
    collected_aircraft_count: int
    published_aircraft_count: int
    truncated_aircraft_count: int
    aircraft_capacity: int
    active_scope_id: str
    coverage: tuple[CoverageAreaSnapshot, ...]


class IngestionService:
    def __init__(
        self,
        provider: AircraftProvider,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.clock = clock
        self.monotonic = monotonic
        self.jitter = jitter
        default_area = TrackingArea(
            latitude_deg=settings.area_latitude_deg,
            longitude_deg=settings.area_longitude_deg,
            radius_nm=settings.area_radius_nm,
        )
        self.plan: CoveragePlan = coverage_plan(settings.coverage_preset, default_area=default_area)
        # Compatibility primary area: it stays fixed for the lifespan and is
        # never the rotating scheduler target.
        self.area = self.plan.areas[0].area
        self._active_scope_index = 0
        self._scope_status: dict[str, CoverageAreaStatus] = {
            scope.identifier: "idle" for scope in self.plan.areas
        }
        self._scope_success: dict[str, datetime | None] = {
            scope.identifier: None for scope in self.plan.areas
        }
        self._scope_error: dict[str, str | None] = {
            scope.identifier: None for scope in self.plan.areas
        }
        self.cache = AircraftCache(
            max_aircraft=settings.max_aircraft,
            retention_seconds=settings.expire_after_seconds,
        )
        self._lock = asyncio.Lock()
        self._next_due = monotonic()
        self._status: ProviderStatus = "idle"
        self._failures = 0
        self._last_error: str | None = None
        self._last_success: datetime | None = None
        self._batch: BatchInfo | None = None
        self._successful_batch_sequence = 0
        self._last_successful_batch: ProviderBatch | None = None

    @property
    def successful_batch_sequence(self) -> int:
        return self._successful_batch_sequence

    @property
    def last_successful_batch(self) -> ProviderBatch | None:
        return self._last_successful_batch

    async def poll_once(self) -> bool:
        """Attempt one due fetch. Concurrent callers and reads cannot multiply requests."""
        async with self._lock:
            self.cache.states(self.clock())
            if self._status == "suspended" or self.monotonic() < self._next_due:
                return False
            # Cancellation must not allow an immediate second request.
            self._next_due = self.monotonic() + self.settings.poll_interval_seconds
            active_scope = self.plan.areas[self._active_scope_index]
            try:
                batch = await self.provider.fetch(active_scope.area)
            except ProviderError as error:
                self._failures = min(self._failures + 1, 31)
                self._last_error = str(error)
                self._scope_status[active_scope.identifier] = "degraded"
                self._scope_error[active_scope.identifier] = str(error)
                if not error.retryable:
                    self._status = "suspended"
                else:
                    self._status = "degraded"
                    # Positive jitter with a 15-minute total cap at the default interval.
                    cap = max(900.0, self.settings.poll_interval_seconds * 1.2)
                    base = min(
                        self.settings.poll_interval_seconds * 2 ** (self._failures - 1),
                        cap / 1.2,
                    )
                    delay = max(self.jitter(base, base * 1.2), error.retry_after_seconds or 0)
                    self._next_due = self.monotonic() + delay
                    # A transient source failure is not a reason to starve all
                    # later sampled hubs. Global backoff still applies before
                    # the next shared request slot.
                    self._active_scope_index = (
                        self._active_scope_index + 1
                    ) % self.plan.cycle_slots
            else:
                self.cache.replace_scope(active_scope.identifier, batch, self.clock())
                self._last_successful_batch = batch
                self._successful_batch_sequence += 1
                self._batch = BatchInfo.model_validate(batch.model_dump(exclude={"aircraft"}))
                self._last_success = self.clock()
                self._failures = 0
                self._last_error = None
                self._status = "healthy"
                self._scope_status[active_scope.identifier] = "healthy"
                self._scope_success[active_scope.identifier] = self.clock()
                self._scope_error[active_scope.identifier] = None
                self._next_due = self.monotonic() + self.settings.poll_interval_seconds
                self._active_scope_index = (self._active_scope_index + 1) % self.plan.cycle_slots
            return True

    def snapshot(self) -> IngestionSnapshot:
        now = self.clock()
        aircraft = self.cache.states(now)
        collected, published, truncated, capacity = self.cache.collection_counts(now)
        stale = sum(
            plane.position_observed_at is None
            or (now - plane.position_observed_at).total_seconds()
            >= self.settings.stale_after_seconds
            for plane in aircraft
        )
        coverage = tuple(
            CoverageAreaSnapshot(
                id=scope.identifier,
                name=scope.name,
                area=scope.area,
                status=self._scope_status[scope.identifier],
                last_success_at=self._scope_success[scope.identifier],
                last_error=self._scope_error[scope.identifier],
                freshness_age_seconds=(
                    (now - self._scope_success[scope.identifier]).total_seconds()
                    if self._scope_success[scope.identifier] is not None
                    else None
                ),
                stale=(
                    self._scope_success[scope.identifier] is None
                    or (now - self._scope_success[scope.identifier]).total_seconds()
                    >= self.settings.stale_after_seconds
                ),
            )
            for scope in self.plan.areas
        )
        return IngestionSnapshot(
            emitted_at=now,
            source=self.provider.source,
            status=self._status,
            last_error=self._last_error,
            consecutive_failures=self._failures,
            last_success_at=self._last_success,
            retry_in_seconds=None
            if self._status == "suspended"
            else max(0.0, self._next_due - self.monotonic()),
            batch=self._batch,
            aircraft=aircraft,
            stale_aircraft_count=stale,
            collected_aircraft_count=collected,
            published_aircraft_count=published,
            truncated_aircraft_count=truncated,
            aircraft_capacity=capacity,
            active_scope_id=self.plan.areas[self._active_scope_index].identifier,
            coverage=coverage,
        )
