"""Lifespan-owned polling and bounded, latest-only subscriber delivery."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import Field

from skytrace.models import FrozenModel, TrackingArea
from skytrace.services.ingestion import IngestionService, IngestionSnapshot

if TYPE_CHECKING:
    from skytrace.history import HistoryStore

logger = logging.getLogger(__name__)
PollerStatus = Literal["running", "failed", "stopped"]
Subscription = asyncio.Queue[str | None]


class TrackingPolicy(FrozenModel):
    area: TrackingArea
    poll_interval_seconds: float
    stale_after_seconds: float
    expire_after_seconds: float
    coverage_preset: Literal["den", "world_hubs_v1"]
    coverage_kind: Literal["single_area", "sampled_hubs"]
    coverage_cycle_seconds: float
    coverage_area_count: int


class SnapshotMessage(FrozenModel):
    type: Literal["snapshot"] = "snapshot"
    schema_version: Literal[1] = 1
    stream_id: str
    sequence: int = Field(ge=0)
    poller_status: PollerStatus
    tracking: TrackingPolicy
    data: IngestionSnapshot


class TelemetryRuntime:
    def __init__(
        self,
        ingestion: IngestionService,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        history: "HistoryStore | None" = None,
        airlabs_snapshot_observer: Callable[[SnapshotMessage], None] | None = None,
        airlabs_observer_failure: Callable[[], None] | None = None,
    ) -> None:
        self.ingestion = ingestion
        self.tracking = TrackingPolicy(
            area=ingestion.area,
            poll_interval_seconds=ingestion.settings.poll_interval_seconds,
            stale_after_seconds=ingestion.settings.stale_after_seconds,
            expire_after_seconds=ingestion.settings.expire_after_seconds,
            coverage_preset=ingestion.plan.preset,
            coverage_kind=ingestion.plan.kind,
            coverage_cycle_seconds=(
                ingestion.settings.poll_interval_seconds * ingestion.plan.cycle_slots
            ),
            coverage_area_count=ingestion.plan.cycle_slots,
        )
        self.sleep = sleep
        self.history = history
        self.airlabs_snapshot_observer = airlabs_snapshot_observer
        self.airlabs_observer_failure = airlabs_observer_failure
        self._history_batch_sequence = ingestion.successful_batch_sequence
        self.stream_id = str(uuid4())
        self.task: asyncio.Task[None] | None = None
        self.poller_status: PollerStatus = "stopped"
        self._subscribers: set[Subscription] = set()
        self._signature: tuple[object, ...] | None = None
        self._sequence = 0
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def start(self) -> None:
        if self.task is not None or self._closed:
            raise RuntimeError("A telemetry runtime can only be started once")
        self.poller_status = "running"
        self.task = asyncio.create_task(self._run(), name="skytrace-poller")

    async def _run(self) -> None:
        try:
            while True:
                await self.ingestion.poll_once()
                if (
                    self.history is not None
                    and self.ingestion.successful_batch_sequence != self._history_batch_sequence
                ):
                    self._history_batch_sequence = self.ingestion.successful_batch_sequence
                    batch = self.ingestion.last_successful_batch
                    if batch is not None:
                        self.history.offer(batch)
                self.snapshot()
                # Check local expiry during Retry-After pauses, without fetching early.
                await self.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Programming failures must be visible, not silently retried as provider outages.
            logger.exception("Telemetry poller stopped unexpectedly")
            self.poller_status = "failed"
            self.snapshot()

    def snapshot(self) -> SnapshotMessage:
        data = self.ingestion.snapshot()
        signature = (
            self.poller_status,
            data.status,
            data.last_error,
            data.consecutive_failures,
            data.last_success_at,
            data.batch,
            data.aircraft,
            data.stale_aircraft_count,
            data.collected_aircraft_count,
            data.published_aircraft_count,
            data.truncated_aircraft_count,
            data.aircraft_capacity,
            data.active_scope_id,
            tuple(
                (scope.id, scope.status, scope.last_success_at, scope.last_error, scope.stale)
                for scope in data.coverage
            ),
        )
        changed = signature != self._signature
        if changed:
            self._signature = signature
            self._sequence += 1
        message = SnapshotMessage(
            stream_id=self.stream_id,
            sequence=self._sequence,
            poller_status=self.poller_status,
            tracking=self.tracking,
            data=data,
        )
        if changed and self.airlabs_snapshot_observer is not None:
            # Synchronous, source-specific continuity observation before publication,
            # including transitions with no websocket subscribers. No provider I/O.
            try:
                self.airlabs_snapshot_observer(message)
            except Exception:
                # Optional context must not interrupt telemetry/history delivery.
                if self.airlabs_observer_failure is not None:
                    try:
                        self.airlabs_observer_failure()
                    except Exception:
                        logger.error("AirLabs observer invalidation failed")
                logger.error("AirLabs snapshot observer failed")
        if changed and self._subscribers:
            serialized = message.model_dump_json()
            for queue in self._subscribers:
                self._offer(queue, serialized)
        return message

    @staticmethod
    def _offer(queue: Subscription, message: str | None) -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(message)

    def subscribe(self) -> Subscription | None:
        if self._closed or len(self._subscribers) >= self.ingestion.settings.ws_max_clients:
            return None
        initial = self.snapshot().model_dump_json()
        queue: Subscription = asyncio.Queue(maxsize=1)
        queue.put_nowait(initial)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: Subscription) -> None:
        self._subscribers.discard(queue)

    async def stop(self) -> None:
        self._closed = True
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.poller_status = "stopped"
        for queue in self._subscribers:
            self._offer(queue, None)
        self._subscribers.clear()
