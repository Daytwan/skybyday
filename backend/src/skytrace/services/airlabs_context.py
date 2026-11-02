"""Isolated selected-aircraft owner. Stage C must explicitly wire this service."""

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from skytrace.models import AircraftObservation
from skytrace.services.airlabs_client import AirLabsClient, LookupResult, ProviderFlight
from skytrace.services.airlabs_models import (
    AirLabsContext,
    ContextCounts,
    ContextReason,
    ContextRow,
    ContextState,
    FlightContextBatch,
    FlightContextCommand,
    FlightContextIntent,
    FlightContextRejection,
    Mode,
    admit_intent,
    ineligible_reason,
    normalize_callsign,
)
from skytrace.services.airlabs_models import (
    AirLabsFlightContextService as DisabledService,
)
from skytrace.services.airlabs_quota import AirLabsQuota, QuotaError, QuotaStatus
from skytrace.services.runtime import SnapshotMessage

UNKNOWN_REASONS = {
    "no_result",
    "identity_mismatch",
    "status_unsupported",
    "missing_fields",
    "invalid_fields",
    "stale_signal",
    "time_mismatch",
    "invalidated",
    "queue_expired",
}


class SubmissionRejected(FlightContextRejection):
    def __init__(self, status: int, reason: ContextReason, retry: float | None = None) -> None:
        super().__init__(status, reason)
        self.retry_in_seconds = retry


@dataclass(eq=False)
class _Slot:
    stream: str
    hex: str
    callsign: str
    generation: int
    freshness_deadline: datetime
    admitted_at: datetime
    admitted_mono: float
    state: ContextState = "queued"
    reason: ContextReason = "queued"
    context: AirLabsContext | None = None
    ends_at: datetime | None = None
    ends_mono: float | None = None
    retry: float | None = None


def match_flight(
    flight: ProviderFlight,
    plane: AircraftObservation,
    dispatch_position: datetime,
    snapshot: SnapshotMessage,
    now: datetime,
    *,
    retrieved_at: datetime | None = None,
) -> tuple[AirLabsContext | None, ContextReason]:
    """Qualified artificial/provider evidence is always unconfirmed."""
    if flight.hex != plane.icao24 or flight.flight_icao != normalize_callsign(plane.callsign):
        return None, "identity_mismatch"
    if ineligible_reason(plane, snapshot, now) is not None:
        return None, "invalidated"
    retrieved_at = retrieved_at or now
    if any(not -15 <= (at - flight.signal_at).total_seconds() <= 120 for at in (retrieved_at, now)):
        return None, "stale_signal"
    positions = (dispatch_position, plane.position_observed_at)
    if any(abs((flight.signal_at - position).total_seconds()) > 90 for position in positions):
        return None, "time_mismatch"
    start, end = flight.departure.time_utc, flight.arrival.time_utc
    if now >= end or any(not start <= point <= end for point in (*positions, flight.signal_at)):
        return None, "time_mismatch"
    expires = min(
        retrieved_at + timedelta(seconds=120),
        flight.signal_at + timedelta(seconds=120),
        end,
        plane.position_observed_at
        + timedelta(seconds=min(snapshot.tracking.stale_after_seconds, 90)),
    )
    if expires <= now:
        # Inclusive evidence boundaries may leave zero display lifetime; do not cache them.
        return None, "not_requested"
    return AirLabsContext(
        retrieved_at=retrieved_at,
        signal_at=flight.signal_at,
        matched_position_at=plane.position_observed_at,
        flight_icao=flight.flight_icao,
        departure=flight.departure,
        arrival=flight.arrival,
        expires_at=expires,
    ), "matched"


class AirLabsFlightContextService:
    def __init__(
        self,
        mode: Mode = "off",
        *,
        snapshot: Callable[[], SnapshotMessage] | None = None,
        client_factory: Callable[[], AirLabsClient] | None = None,
        quota_factory: Callable[[], AirLabsQuota] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.mode, self.clock, self.monotonic, self.sleep = mode, clock, monotonic, sleep
        self._snapshot = snapshot
        self._client_factory, self._quota_factory = client_factory, quota_factory
        self._client: AirLabsClient | None = None
        self._quota: AirLabsQuota | None = None
        self._worker: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._slots: dict[str, _Slot] = {}
        self._queue: deque[_Slot] = deque()
        self._inflight: _Slot | None = None
        self._generation = 0
        self._observed: tuple[str, int] | None = None
        self._running = False
        self._closed = False
        self._clock_pending = False
        self._unavailable: ContextReason = "lifecycle_unavailable"

    def start(self) -> None:
        if self.mode == "off" or self._closed or self._running:
            return
        if self._snapshot is None or self._quota_factory is None or self._client_factory is None:
            return
        try:
            asyncio.get_running_loop()
            self._quota = self._quota_factory()
            self._quota.start()
            status = self._quota.status(self.clock())
            if status.reason not in {None, "cooldown", "rate_limited", "local_budget"}:
                self._unavailable = status.reason
                self._quota.close()
                return
            self._client = self._client_factory()
            self._running = True
            self._worker = asyncio.create_task(self._run(), name="airlabs-selected-context")
        except Exception:
            self._unavailable = "lifecycle_unavailable"
            self._running = False
            if self._quota is not None:
                try:
                    self._quota.close()
                except Exception:
                    pass

    async def stop(self) -> None:
        self._closed = True
        self._running = False
        self.invalidate()
        try:
            if self._worker is not None:
                self._worker.cancel()
                try:
                    await self._worker
                except (asyncio.CancelledError, Exception):
                    pass
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:
                    pass
        finally:
            if self._quota is not None:
                try:
                    self._quota.close()
                except Exception:
                    pass
            self._worker = self._client = None
            self._inflight = None

    def invalidate(self) -> None:
        """Synchronous fail-closed hook for a Stage C observer/lifecycle failure."""
        self._slots.clear()
        self._queue.clear()
        self._generation += 1
        self._wake.set()

    def observer_failed(self) -> None:
        self.invalidate()
        self._running = False
        self._unavailable = "lifecycle_unavailable"

    def _remove(self, slot: _Slot) -> None:
        if self._slots.get(slot.hex) is slot:
            del self._slots[slot.hex]
        self._queue = deque(item for item in self._queue if item is not slot)
        slot.context = None

    def observe_snapshot(self, message: SnapshotMessage) -> None:
        """No I/O or await: observe every changed runtime transition before publication."""
        if not self._running:
            return
        try:
            observed = (message.stream_id, message.sequence)
            if observed == self._observed:
                return
            if (
                self._observed
                and observed[0] == self._observed[0]
                and observed[1] < self._observed[1]
            ):
                self.observer_failed()
                return
            now = self.clock()
            planes = {plane.icao24: plane for plane in message.data.aircraft}
            for slot in tuple(self._slots.values()):
                plane = planes.get(slot.hex)
                if (
                    slot.stream != message.stream_id
                    or plane is None
                    or slot.freshness_deadline < now
                    or normalize_callsign(plane.callsign) != slot.callsign
                    or ineligible_reason(plane, message, now) is not None
                ):
                    self._remove(slot)
                else:
                    slot.freshness_deadline = plane.position_observed_at + timedelta(
                        seconds=min(message.tracking.stale_after_seconds, 90)
                    )
            self._observed = observed
        except Exception:
            self.observer_failed()

    def _sweep(self, snapshot: SnapshotMessage) -> None:
        self.observe_snapshot(snapshot)
        if not self._running:
            return
        now, mono = self.clock(), self.monotonic()
        if not self._quota.clock_valid(now) or not math.isfinite(mono):
            self.invalidate()
            self._clock_pending = True
            self._wake.set()
            return
        planes = {plane.icao24: plane for plane in snapshot.data.aircraft}
        for slot in tuple(self._slots.values()):
            plane = planes.get(slot.hex)
            if slot.state == "queued" and (
                (now - slot.admitted_at).total_seconds() >= 90 or mono - slot.admitted_mono >= 90
            ):
                self._remove(slot)
                continue
            if (
                plane is None
                or slot.freshness_deadline < now
                or ineligible_reason(plane, snapshot, now) is not None
            ):
                self._remove(slot)
                continue
            if slot.ends_at is not None and (
                now >= slot.ends_at or mono >= slot.ends_mono or mono < slot.admitted_mono
            ):
                self._remove(slot)
                continue
            context = slot.context
            if context is not None:
                deadline = min(context.expires_at, slot.freshness_deadline)
                if (
                    now >= deadline
                    or abs((context.signal_at - plane.position_observed_at).total_seconds()) > 90
                    or not context.departure.time_utc
                    <= plane.position_observed_at
                    <= context.arrival.time_utc
                ):
                    self._remove(slot)
                elif deadline < context.expires_at:
                    slot.context = context.model_copy(update={"expires_at": deadline})
                    slot.ends_at = deadline

    def _health(self) -> QuotaStatus:
        if self.mode == "off":
            return QuotaStatus("disabled")
        if not self._running:
            return QuotaStatus(self._unavailable)
        return self._quota.status(self.clock())

    def submit(
        self, intent: FlightContextIntent, snapshot: SnapshotMessage
    ) -> FlightContextCommand:
        admit_intent(intent, snapshot, self.clock())
        self._sweep(snapshot)
        slot = self._slots.get(intent.icao24)
        if slot is not None:
            return self._command(slot, snapshot)
        status = self._health()
        if status.reason not in {None, "cooldown"}:
            code = (
                429
                if status.reason in {"local_budget", "rate_limited", "allocation_exhausted"}
                else 503
            )
            raise SubmissionRejected(code, status.reason, status.retry_in_seconds)
        if len(self._queue) >= 8:
            raise SubmissionRejected(429, "queue_full")
        detached_inflight = self._inflight is not None and (
            self._slots.get(self._inflight.hex) is not self._inflight
        )
        if len(self._slots) + int(detached_inflight) >= 64:
            raise SubmissionRejected(429, "capacity")
        plane = next(plane for plane in snapshot.data.aircraft if plane.icao24 == intent.icao24)
        self._generation += 1
        slot = _Slot(
            stream=snapshot.stream_id,
            hex=plane.icao24,
            callsign=intent.callsign,
            generation=self._generation,
            freshness_deadline=plane.position_observed_at
            + timedelta(seconds=min(snapshot.tracking.stale_after_seconds, 90)),
            admitted_at=self.clock(),
            admitted_mono=self.monotonic(),
        )
        self._slots[slot.hex] = slot
        self._queue.append(slot)
        self._wake.set()
        return self._command(slot, snapshot)

    @staticmethod
    def _command(slot: _Slot, snapshot: SnapshotMessage) -> FlightContextCommand:
        return FlightContextCommand(
            stream_id=snapshot.stream_id,
            sequence=snapshot.sequence,
            icao24=slot.hex,
            callsign=slot.callsign,
            state=slot.state,
            reason=slot.reason,
            retry_in_seconds=slot.retry,
            context=slot.context,
        )

    def project(self, snapshot: SnapshotMessage) -> FlightContextBatch:
        self._sweep(snapshot)
        base = DisabledService(self.mode, clock=self.clock).project(snapshot)
        status = self._health()
        if self.mode == "off":
            return base
        rows = []
        for row in base.contexts:
            slot = self._slots.get(row.icao24)
            if row.state != "ineligible" and slot is not None:
                row = ContextRow(
                    **{
                        **row.model_dump(),
                        "state": slot.state,
                        "reason": slot.reason,
                        "retry_in_seconds": slot.retry,
                        "context": slot.context,
                    }
                )
            rows.append(row)
        counts = {name: 0 for name in ContextCounts.model_fields}
        for row in rows:
            counts[row.state] += 1
        service_state = "ready"
        if status.reason in {"cooldown", "rate_limited", "local_budget"}:
            service_state = "cooldown"
        elif status.reason in {"ledger_unavailable", "lifecycle_unavailable"}:
            service_state = "unavailable"
        elif status.reason is not None:
            service_state = "suspended"
        return FlightContextBatch(
            stream_id=snapshot.stream_id,
            sequence=snapshot.sequence,
            emitted_at=self.clock(),
            mode=self.mode,
            service_state=service_state,
            service_reason=status.reason,
            tracked_aircraft=len(rows),
            counts=ContextCounts(**counts),
            two_endpoint_contexts=counts["matched"],
            contexts=rows,
        )

    def _negative(self, slot: _Slot, reason: ContextReason, retry: float | None = None) -> None:
        slot.state = "unknown" if reason in UNKNOWN_REASONS else "unavailable"
        slot.reason, slot.retry = reason, retry
        slot.context = None
        slot.ends_at = self.clock() + timedelta(seconds=60)
        slot.ends_mono = self.monotonic() + 60

    async def _wait_expiry(self) -> None:
        """Compute remaining lifetime when scheduled, then wait only for deletion."""
        now, mono = self.clock(), self.monotonic()
        delays = [
            max(0, min((slot.ends_at - now).total_seconds(), slot.ends_mono - mono))
            for slot in self._slots.values()
            if slot.ends_at is not None
        ]
        if delays and min(delays) > 0:
            await self.sleep(min(delays))

    async def _await_with_expiry(self, operation: Awaitable):
        """Keep deleting expired payloads while one already-owned operation waits."""
        task = asyncio.ensure_future(operation)
        try:
            while not task.done():
                if not any(slot.ends_at is not None for slot in self._slots.values()):
                    return await task
                expiry = asyncio.create_task(self._wait_expiry())
                try:
                    await asyncio.wait((task, expiry), return_when=asyncio.FIRST_COMPLETED)
                    if expiry.done():
                        expiry.result()
                        self._sweep(self._snapshot())
                finally:
                    expiry.cancel()
                    await asyncio.gather(expiry, return_exceptions=True)
            return task.result()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _wait_idle(self) -> None:
        """Wake only for commands or deletion deadlines, never for reacquisition."""
        self._wake.clear()
        await self._await_with_expiry(self._wake.wait())

    async def _run(self) -> None:
        try:
            while self._running:
                if self._clock_pending:
                    self._quota.record_failure("clock_untrusted", self.clock())
                    self._clock_pending = False
                    self.invalidate()
                if not self._queue:
                    await self._wait_idle()
                    continue
                snapshot = self._snapshot()
                self._sweep(snapshot)
                if not self._queue:
                    del snapshot
                    continue
                slot = self._queue[0]
                now = self.clock()
                remaining = min(
                    90 - (now - slot.admitted_at).total_seconds(),
                    90 - (self.monotonic() - slot.admitted_mono),
                )
                if remaining <= 0:
                    self._queue.popleft()
                    self._negative(slot, "queue_expired")
                    del snapshot, slot
                    continue
                status = self._quota.status(now)
                if status.reason in {"cooldown", "rate_limited"} and status.retry_in_seconds:
                    delay = min(status.retry_in_seconds, remaining)
                    del snapshot, slot
                    await self._await_with_expiry(self.sleep(delay))
                    continue
                self._queue.popleft()
                try:
                    self._quota.reserve(now)
                except QuotaError as error:
                    self._negative(slot, error.reason, error.retry_in_seconds)
                    del snapshot, slot
                    continue
                plane = next(plane for plane in snapshot.data.aircraft if plane.icao24 == slot.hex)
                dispatch_position = plane.position_observed_at
                del plane, snapshot
                slot.state, slot.reason = "pending", "pending"
                self._inflight = slot
                result = await self._await_with_expiry(self._client.lookup(slot.callsign))
                retrieved_at, retrieved_mono = self.clock(), self.monotonic()
                self._quota.record_failure(
                    result.reason or "matched", retrieved_at, result.retry_after_seconds
                )
                snapshot = self._snapshot()
                self._sweep(snapshot)
                if self._running and self._slots.get(slot.hex) is slot:
                    self._complete(
                        slot, result, snapshot, dispatch_position, retrieved_at, retrieved_mono
                    )
                self._inflight = None
                # The idle worker must retain neither a provider result nor a cohort snapshot.
                del result, snapshot, slot
        except asyncio.CancelledError:
            raise
        except Exception:
            self.observer_failed()

    def _complete(
        self,
        slot: _Slot,
        result: LookupResult,
        snapshot: SnapshotMessage,
        dispatch_position: datetime,
        retrieved_at: datetime,
        retrieved_mono: float,
    ) -> None:
        if result.flight is None:
            self._negative(slot, result.reason or "source_error")
            return
        plane = next(plane for plane in snapshot.data.aircraft if plane.icao24 == slot.hex)
        now = self.clock()
        context, reason = match_flight(
            result.flight,
            plane,
            dispatch_position,
            snapshot,
            now,
            retrieved_at=retrieved_at,
        )
        if reason == "not_requested":
            self._remove(slot)
        elif context is None:
            self._negative(slot, reason)
        else:
            slot.state, slot.reason, slot.context = "matched", "matched", context
            slot.ends_at = context.expires_at
            slot.ends_mono = min(
                retrieved_mono + 120,
                self.monotonic() + (context.expires_at - now).total_seconds(),
            )
