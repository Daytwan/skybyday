"""Artificial in-process owner tests. No application wiring or live source calls."""

import asyncio
import json
import socket
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from test_airlabs_client import artificial_response
from test_airlabs_models import artificial_snapshot
from test_airlabs_quota import Clock, artificial_seed, contents

from skytrace.services.airlabs_client import AirLabsClient, LookupResult, normalize_response
from skytrace.services.airlabs_context import (
    AirLabsFlightContextService,
    SubmissionRejected,
    match_flight,
)
from skytrace.services.airlabs_models import FlightContextBatch, FlightContextIntent
from skytrace.services.airlabs_quota import AirLabsQuota, QuotaStatus, seed_ledger


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Real networking is forbidden in artificial AirLabs tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


class World:
    def __init__(self, clock, count=1, stale=90):
        self.clock = clock
        self.message = artificial_snapshot(count)
        self.message = self.message.model_copy(
            update={
                "tracking": self.message.tracking.model_copy(update={"stale_after_seconds": stale})
            }
        )
        self.refresh()

    def refresh(self):
        planes = tuple(
            plane.model_copy(
                update={
                    "callsign": f"ZZZ{index:03}",
                    "provider_timestamp": self.clock.now,
                    "received_at": self.clock.now,
                    "position_observed_at": self.clock.now,
                }
            )
            for index, plane in enumerate(self.message.data.aircraft)
        )
        self.publish(planes)

    def publish(self, planes, **changes):
        self.message = self.message.model_copy(
            update={
                "sequence": self.message.sequence + 1,
                "data": self.message.data.model_copy(update={"aircraft": tuple(planes)}),
                **changes,
            }
        )

    def change(self, **changes):
        planes = list(self.message.data.aircraft)
        planes[0] = planes[0].model_copy(update=changes)
        self.publish(planes)

    def intent(self, index=0):
        plane = self.message.data.aircraft[index]
        return FlightContextIntent(
            schema_version=1,
            stream_id=self.message.stream_id,
            sequence=self.message.sequence - 1,
            source="adsblol",
            icao24=plane.icao24,
            callsign=plane.callsign,
            position_observed_at=plane.position_observed_at,
            view="live",
        )


class FakeQuota:
    """Service-only scheduling seam; real policy/counters are tested with SQLite separately."""

    def __init__(self):
        self.reservations = 0
        self.current = QuotaStatus()
        self.valid_clock = True
        self.closed = False
        self.failures = []

    def start(self):
        pass

    def close(self):
        self.closed = True

    def status(self, now):
        return self.current

    def clock_valid(self, now):
        return self.valid_clock

    def reserve(self, now):
        self.reservations += 1
        return self.reservations

    def record_failure(self, reason, now, retry_after=None):
        self.failures.append(reason)
        if reason in {"authentication", "month_exhausted", "entitlement", "clock_untrusted"}:
            self.current = QuotaStatus(reason)


class FakeClient:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.gate = None
        self.result = None
        self.closed = False

    async def lookup(self, callsign):
        self.calls.append(callsign)
        if self.gate is not None:
            await self.gate.wait()
        if self.result is not None:
            return self.result
        plane = next(
            plane for plane in self.world.message.data.aircraft if plane.callsign == callsign
        )
        return normalize_response(
            json.dumps(
                artificial_response(
                    hex=plane.icao24,
                    flight_icao=callsign,
                    updated=int(self.world.clock.now.timestamp()),
                    dep_time_ts=int((self.world.clock.now - timedelta(hours=1)).timestamp()),
                    arr_time_ts=int((self.world.clock.now + timedelta(hours=1)).timestamp()),
                )
            ).encode()
        )

    async def close(self):
        self.closed = True


class ControlledSleep:
    """Advance deletion/slot waits with the same artificial UTC and monotonic clock."""

    def __init__(self, clock):
        self.clock = clock
        self.waiters = []

    async def __call__(self, delay):
        if delay <= 0:
            return
        future = asyncio.get_running_loop().create_future()
        entry = (self.clock.mono + delay, future)
        self.waiters.append(entry)
        try:
            await future
        finally:
            self.waiters.remove(entry)

    def advance(self, seconds):
        self.clock.advance(seconds)
        for deadline, future in tuple(self.waiters):
            if deadline <= self.clock.mono and not future.done():
                future.set_result(None)


def owner(world, *, client=None, quota=None, sleep=asyncio.sleep):
    client, quota = client or FakeClient(world), quota or FakeQuota()
    service = AirLabsFlightContextService(
        "selected",
        snapshot=lambda: world.message,
        client_factory=lambda: client,
        quota_factory=lambda: quota,
        clock=lambda: world.clock.now,
        monotonic=lambda: world.clock.mono,
        sleep=sleep,
    )
    service.start()
    return service, client, quota


async def wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def row(service, world, index=0):
    return service.project(world.message).contexts[index]


@pytest.mark.anyio
async def test_full_400_cohort_and_coalescing_at_queue_inflight_and_cache():
    clock = Clock()
    world = World(clock, 400)
    world.change(source="synthetic")
    service, client, quota = owner(world)
    client.gate = asyncio.Event()
    try:
        intent = world.intent(1)
        assert service.submit(intent, world.message).state == "queued"
        assert service.submit(intent, world.message).state == "queued"
        await wait_for(lambda: len(client.calls) == 1)
        assert service.submit(intent, world.message).state == "pending"
        client.gate.set()
        await wait_for(lambda: row(service, world, 1).state == "matched")
        for _ in range(10):
            assert service.submit(intent, world.message).state == "matched"
            batch = service.project(world.message)
        assert len(client.calls) == quota.reservations == 1
        assert batch.tracked_aircraft == len(batch.contexts) == 400
        assert batch.counts.matched == batch.two_endpoint_contexts == 1
        assert batch.counts.ineligible == 1 and batch.counts.not_requested == 398
        assert batch.confirmed_current_flights == 0
        assert FlightContextBatch.model_validate_json(batch.model_dump_json()) == batch
    finally:
        await service.stop()


@pytest.mark.anyio
async def test_newer_sequence_and_position_commit_then_reads_do_not_extend_expiry():
    clock = Clock()
    world = World(clock)
    service, client, _ = owner(world)
    client.gate = asyncio.Event()
    try:
        service.submit(world.intent(), world.message)
        await wait_for(lambda: len(client.calls) == 1)
        clock.advance(10)
        world.refresh()
        service.observe_snapshot(world.message)
        client.gate.set()
        await wait_for(lambda: row(service, world).state == "matched")
        context = row(service, world).context
        assert context.matched_position_at == clock.now
        clock.advance(20)
        world.refresh()
        assert row(service, world).context.expires_at == context.expires_at
        # Supply fresh positions until the original expiry; reads never renew it.
        clock.advance(60)
        world.refresh()
        service.observe_snapshot(world.message)
        clock.advance(10)
        world.refresh()
        assert row(service, world).state == "not_requested"
        assert service._slots == {} and len(client.calls) == 1
    finally:
        await service.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("transition", ["callsign", "source", "stream", "hex", "absent", "stale"])
async def test_intermediate_identity_transitions_prevent_old_inflight_commit(transition):
    clock = Clock()
    world = World(clock)
    service, client, quota = owner(world)
    client.gate = asyncio.Event()
    original = world.message.data.aircraft
    try:
        service.submit(world.intent(), world.message)
        await wait_for(lambda: len(client.calls) == 1)
        if transition == "callsign":
            world.change(callsign="YYY123")
        elif transition == "source":
            world.change(source="synthetic")
        elif transition == "stream":
            world.publish(original, stream_id="another-artificial-stream")
        elif transition == "hex":
            world.change(icao24="ffffff")
        elif transition == "absent":
            world.publish([])
        else:
            clock.advance(91)
            world.refresh()  # Old freshness already expired before this newer position.
        service.observe_snapshot(world.message)
        world.publish(original)
        world.refresh()
        service.observe_snapshot(world.message)
        client.gate.set()
        await wait_for(lambda: service._inflight is None)
        assert row(service, world).state == "not_requested"
        assert quota.reservations == len(client.calls) == 1
    finally:
        await service.stop()


@pytest.mark.anyio
async def test_observer_failure_discards_old_result_and_isolates_shutdown_failure():
    clock = Clock()
    world = World(clock)
    service, client, quota = owner(world)
    client.gate = asyncio.Event()
    service.submit(world.intent(), world.message)
    await wait_for(lambda: len(client.calls) == 1)
    service.observer_failed()
    client.gate.set()
    await wait_for(lambda: service._worker.done())
    assert service.project(world.message).service_state == "unavailable"
    assert row(service, world).context is None

    async def close_failure():
        raise RuntimeError("PRIVATE artificial close failure")

    client.close = close_failure
    await service.stop()
    assert quota.closed


@pytest.mark.anyio
async def test_queue_eight_plus_inflight_and_shutdown_never_refund_or_retry():
    world = World(Clock(), 10)
    service, client, quota = owner(world)
    client.gate = asyncio.Event()
    service.submit(world.intent(), world.message)
    await wait_for(lambda: len(client.calls) == 1)
    for index in range(1, 9):
        assert service.submit(world.intent(index), world.message).state == "queued"
    with pytest.raises(SubmissionRejected, match="queue_full"):
        service.submit(world.intent(9), world.message)
    assert len(service._slots) == 9 and quota.reservations == 1
    await service.stop()
    assert client.closed and quota.closed and service._slots == {}
    assert quota.reservations == 1 and len(client.calls) == 1


@pytest.mark.anyio
async def test_expired_waiting_job_is_discarded_before_reservation():
    clock = Clock()
    world = World(clock)
    gate = asyncio.Event()

    async def sleep(_):
        await gate.wait()

    service, client, quota = owner(world, sleep=sleep)
    quota.current = QuotaStatus("cooldown", 60)
    try:
        service.submit(world.intent(), world.message)
        await asyncio.sleep(0)
        clock.advance(60)
        world.refresh()
        service.observe_snapshot(world.message)
        clock.advance(30)
        world.refresh()
        quota.current = QuotaStatus()
        gate.set()
        await wait_for(lambda: row(service, world).state == "not_requested")
        assert quota.reservations == 0 and client.calls == []
    finally:
        await service.stop()


@pytest.mark.anyio
async def test_64_total_slots_include_negatives_and_reads_do_not_evict_cooldown():
    clock = Clock()
    world = World(clock, 65)
    service, client, quota = owner(world)
    client.result = LookupResult(reason="no_result")
    try:
        for index in range(64):
            service.submit(world.intent(index), world.message)
            await wait_for(lambda index=index: row(service, world, index).state == "unknown")
        with pytest.raises(SubmissionRejected, match="capacity"):
            service.submit(world.intent(64), world.message)
        for _ in range(5):
            assert service.submit(world.intent(), world.message).state == "unknown"
        assert len(service._slots) == 64 and quota.reservations == 64
        clock.advance(60)
        world.refresh()
        assert service.submit(world.intent(64), world.message).state == "queued"
        await wait_for(lambda: row(service, world, 64).state == "unknown")
        assert quota.reservations == 65
    finally:
        await service.stop()


@pytest.mark.parametrize(
    ("signal_age", "position_age", "expected"),
    [
        (-15, 0, "matched"),
        (-16, 0, "stale_signal"),
        (120, 30, "not_requested"),
        (121, 31, "stale_signal"),
        (90, 0, "matched"),
        (91, 0, "time_mismatch"),
        (0, 90, "not_requested"),
        (0, 91, "invalidated"),
    ],
)
def test_signal_position_and_separation_boundaries(signal_age, position_age, expected):
    clock = Clock()
    world = World(clock)
    world.change(position_observed_at=clock.now - timedelta(seconds=position_age))
    plane = world.message.data.aircraft[0]
    flight = normalize_response(
        json.dumps(
            artificial_response(
                hex=plane.icao24,
                flight_icao=plane.callsign,
                updated=int((clock.now - timedelta(seconds=signal_age)).timestamp()),
            )
        ).encode()
    ).flight
    context, reason = match_flight(
        flight, plane, plane.position_observed_at, world.message, clock.now
    )
    assert reason == expected
    assert (context is not None) == (expected == "matched")


@pytest.mark.parametrize(
    "changes",
    [
        {"hex": "bbbbbb"},
        {"flight_icao": "ZZZ001"},
        {"dep_time_ts": 1793012400, "arr_time_ts": 1793019600},  # Earlier artificial date.
    ],
)
def test_same_callsign_or_endpoints_cannot_compensate_for_hex_or_date(changes):
    clock = Clock()
    world = World(clock)
    plane = world.message.data.aircraft[0]
    payload = artificial_response(hex=plane.icao24, flight_icao=plane.callsign)
    payload["response"].update(changes)
    flight = normalize_response(json.dumps(payload).encode()).flight
    context, reason = match_flight(
        flight, plane, plane.position_observed_at, world.message, clock.now
    )
    assert context is None and reason in {"identity_mismatch", "time_mismatch"}


@pytest.mark.anyio
async def test_commit_rechecks_dispatch_and_latest_position_window():
    clock = Clock()
    world = World(clock)
    service, client, _ = owner(world)
    client.gate = asyncio.Event()
    client.result = normalize_response(
        json.dumps(
            artificial_response(
                hex="000000",
                flight_icao="ZZZ000",
                updated=int((clock.now - timedelta(seconds=85)).timestamp()),
            )
        ).encode()
    )
    try:
        service.submit(world.intent(), world.message)
        await wait_for(lambda: len(client.calls) == 1)
        clock.advance(6)
        world.refresh()
        client.gate.set()
        await wait_for(lambda: row(service, world).state == "unknown")
        assert row(service, world).reason == "time_mismatch"
    finally:
        await service.stop()


@pytest.mark.anyio
async def test_default_off_incomplete_start_and_start_failure_construct_no_client():
    world = World(Clock())
    calls = []

    def forbidden():
        calls.append("factory")
        raise RuntimeError("ARTIFICIAL")

    for mode, quota_factory in (("off", forbidden), ("selected", None), ("selected", forbidden)):
        service = AirLabsFlightContextService(
            mode,
            snapshot=lambda: world.message,
            client_factory=forbidden,
            quota_factory=quota_factory,
            clock=lambda: world.clock.now,
        )
        service.start()
        with pytest.raises(SubmissionRejected) as error:
            service.submit(world.intent(), world.message)
        assert error.value.status == 503
        assert service.project(world.message).confirmed_current_flights == 0
        await service.stop()
    assert calls == ["factory"]  # Only the failing quota constructor ran.


@pytest.mark.anyio
async def test_clock_stop_clears_waiting_and_persists_without_get_acquisition():
    world = World(Clock())
    service, client, quota = owner(world)
    try:
        service.submit(world.intent(), world.message)
        quota.valid_clock = False
        assert row(service, world).context is None
        await wait_for(lambda: "clock_untrusted" in quota.failures)
        assert client.calls == [] and quota.reservations == 0
        assert service.project(world.message).service_reason == "clock_untrusted"
    finally:
        await service.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("commit_failure", [False, True])
async def test_real_ledger_commits_before_mock_transport_and_failure_spends_no_http(
    tmp_path,
    monkeypatch,
    commit_failure,
):
    clock = Clock()
    path = tmp_path / "integration.sqlite"
    seed_ledger(path, artificial_seed(), now=clock.now)
    quota = AirLabsQuota(path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    world = World(clock)
    requests = []

    def transport(request):
        assert len(contents(path)[1]) == 1  # Durably visible before the transport starts.
        requests.append(request)
        return httpx.Response(
            200,
            json=artificial_response(
                hex="000000", flight_icao="ZZZ000", updated=int(clock.now.timestamp())
            ),
        )

    client = AirLabsClient(SecretStr("ARTIFICIAL"), transport=httpx.MockTransport(transport))
    service, _, _ = owner(world, client=client, quota=quota)
    try:
        clock.advance(60)
        world.refresh()
        if commit_failure:

            def broken():
                raise OSError("ARTIFICIAL commit failure")

            monkeypatch.setattr(quota, "_commit", broken)
        service.submit(world.intent(), world.message)
        await wait_for(lambda: row(service, world).state not in {"queued", "pending"})
        assert len(requests) == (0 if commit_failure else 1)
        assert row(service, world).state == ("unavailable" if commit_failure else "matched")
    finally:
        await service.stop()


@pytest.mark.anyio
async def test_idle_expiry_deletes_payload_without_reads_updates_or_reacquisition():
    import weakref

    clock = Clock()
    world = World(clock)
    expire = asyncio.Event()
    delays = []

    async def sleep(delay):
        delays.append(delay)
        await expire.wait()

    service, client, quota = owner(world, sleep=sleep)
    lookup = client.lookup
    references = []

    async def tracked_lookup(callsign):
        result = await lookup(callsign)
        references.append(weakref.ref(result.flight))
        return result

    client.lookup = tracked_lookup
    try:
        service.submit(world.intent(), world.message)
        await wait_for(lambda: bool(delays))
        assert references[0]() is None  # Worker released the normalized response object.
        assert delays == [90]
        clock.advance(90)
        expire.set()
        await wait_for(lambda: not service._slots)  # No GET/observer call triggers this cleanup.
        assert quota.reservations == len(client.calls) == 1
    finally:
        await service.stop()


@pytest.mark.anyio
async def test_busy_cooldown_wait_deletes_expired_payload_without_projection(tmp_path):
    import weakref

    clock = Clock()
    world = World(clock, 3)
    sleeper = ControlledSleep(clock)
    path = tmp_path / "retention.sqlite"
    seed_ledger(path, artificial_seed(), now=clock.now)
    quota = AirLabsQuota(path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    service, client, _ = owner(world, quota=quota, sleep=sleeper)
    try:
        sleeper.advance(60)
        world.refresh()
        service.observe_snapshot(world.message)
        service.submit(world.intent(0), world.message)
        await wait_for(lambda: row(service, world).state == "matched")
        context = row(service, world).context
        retrieved, expires = context.retrieved_at, context.expires_at
        reference = weakref.ref(context)
        del context

        sleeper.advance(60)
        world.refresh()
        service.observe_snapshot(world.message)
        client.result = LookupResult(reason="rate_limited", retry_after_seconds=3600)
        service.submit(world.intent(1), world.message)
        service.submit(world.intent(2), world.message)
        await wait_for(
            lambda: len(client.calls) == 2 and quota.status(clock.now).reason == "rate_limited"
        )
        sleeper.advance(61)
        world.refresh()
        service.observe_snapshot(world.message)
        await wait_for(lambda: reference() is None)  # No GET drives deletion.
        assert clock.now > expires and (clock.now - retrieved).total_seconds() == 121
        assert len(client.calls) == len(contents(path)[1]) == 2
    finally:
        await service.stop()
    assert sleeper.waiters == []


@pytest.mark.anyio
async def test_inflight_lookup_keeps_expiry_cleanup_and_shutdown_cancels_owned_waits(tmp_path):
    import weakref

    clock = Clock()
    world = World(clock, 2)
    sleeper = ControlledSleep(clock)
    path = tmp_path / "inflight-expiry.sqlite"
    seed_ledger(path, artificial_seed(), now=clock.now)
    quota = AirLabsQuota(path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    service, client, _ = owner(world, quota=quota, sleep=sleeper)
    try:
        sleeper.advance(60)
        world.refresh()
        service.submit(world.intent(0), world.message)
        await wait_for(lambda: row(service, world).state == "matched")
        reference = weakref.ref(row(service, world).context)
        sleeper.advance(80)
        world.refresh()
        service.observe_snapshot(world.message)
        client.gate = asyncio.Event()
        service.submit(world.intent(1), world.message)
        await wait_for(lambda: len(client.calls) == 2 and bool(sleeper.waiters))
        sleeper.advance(10)
        # Neither GET nor a runtime observation is needed at the expiry instant.
        await wait_for(lambda: reference() is None)
        assert len(client.calls) == len(contents(path)[1]) == 2
        assert not client.gate.is_set() and service._inflight is not None
    finally:
        await service.stop()
    assert client.closed and sleeper.waiters == []
    assert len(contents(path)[1]) == 2  # Cancellation still never refunds a reservation.


@pytest.mark.parametrize(
    ("departure_offset", "arrival_offset", "signal_offset", "reason"),
    [
        (1, 3600, 0, "time_mismatch"),
        (-3600, 0, 0, "time_mismatch"),
        (-1, 3600, -2, "time_mismatch"),
        (-3600, 10, 0, "matched"),
    ],
)
def test_window_boundaries_and_arrival_expiry(
    departure_offset,
    arrival_offset,
    signal_offset,
    reason,
):
    clock = Clock()
    world = World(clock)
    plane = world.message.data.aircraft[0]
    flight = normalize_response(
        json.dumps(
            artificial_response(
                hex=plane.icao24,
                flight_icao=plane.callsign,
                dep_time_ts=int((clock.now + timedelta(seconds=departure_offset)).timestamp()),
                arr_time_ts=int((clock.now + timedelta(seconds=arrival_offset)).timestamp()),
                updated=int((clock.now + timedelta(seconds=signal_offset)).timestamp()),
                arr_iata=None,
                arr_icao="KZZZ",  # Same-airport flight is allowed.
            )
        ).encode()
    ).flight
    context, outcome = match_flight(
        flight,
        plane,
        plane.position_observed_at,
        world.message,
        clock.now,
    )
    assert outcome == reason
    if context is not None:
        assert context.expires_at == clock.now + timedelta(seconds=10)
        assert context.current_flight_confirmed is False and context.provider_leg_id is None


def test_position_age_uses_smaller_tracking_limit_and_dispatch_separation():
    clock = Clock()
    world = World(clock, stale=30)
    plane = world.message.data.aircraft[0]
    flight = normalize_response(
        json.dumps(
            artificial_response(
                hex=plane.icao24,
                flight_icao=plane.callsign,
            )
        ).encode()
    ).flight
    context, _ = match_flight(flight, plane, plane.position_observed_at, world.message, clock.now)
    assert context.expires_at == clock.now + timedelta(seconds=30)
    for age, expected in ((30, "not_requested"), (31, "invalidated")):
        older = plane.model_copy(
            update={"position_observed_at": clock.now - timedelta(seconds=age)}
        )
        assert (
            match_flight(flight, older, older.position_observed_at, world.message, clock.now)[1]
            == expected
        )
    assert (
        match_flight(flight, plane, clock.now - timedelta(seconds=91), world.message, clock.now)[1]
        == "time_mismatch"
    )


def test_receipt_and_commit_both_validate_signal_age():
    clock = Clock()
    world = World(clock)
    plane = world.message.data.aircraft[0]
    flight = normalize_response(
        json.dumps(
            artificial_response(
                hex=plane.icao24,
                flight_icao=plane.callsign,
                updated=int((clock.now + timedelta(seconds=10)).timestamp()),
            )
        ).encode()
    ).flight
    assert (
        match_flight(
            flight,
            plane,
            plane.position_observed_at,
            world.message,
            clock.now,
            retrieved_at=clock.now - timedelta(seconds=6),
        )[1]
        == "stale_signal"
    )


@pytest.mark.anyio
async def test_invalidated_inflight_still_counts_toward_total_memory_capacity():
    world = World(Clock(), 65)
    service, client, _ = owner(world)
    client.result = LookupResult(reason="no_result")
    try:
        for index in range(1, 64):
            service.submit(world.intent(index), world.message)
            await wait_for(lambda index=index: row(service, world, index).state == "unknown")
        client.gate = asyncio.Event()
        service.submit(world.intent(), world.message)
        await wait_for(lambda: row(service, world).state == "pending")
        world.change(callsign="YYY001")
        service.observe_snapshot(world.message)
        with pytest.raises(SubmissionRejected, match="capacity"):
            service.submit(world.intent(64), world.message)
        client.gate.set()
        await wait_for(lambda: service._inflight is None)
        assert service.submit(world.intent(64), world.message).state == "queued"
    finally:
        await service.stop()
