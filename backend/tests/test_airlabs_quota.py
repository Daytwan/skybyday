"""Disposable local ledgers and fake clocks; no provider or live accounting input."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from skytrace.services.airlabs_quota import (
    DAY,
    AirLabsQuota,
    QuotaError,
    reconcile_ledger,
    seed_ledger,
)

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = NOW
        self.mono = 0.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


def artificial_seed(**changes):
    seed = dict(
        schema_version=1,
        verified_at_utc=NOW.isoformat(),
        window_start_utc=(NOW - timedelta(days=1)).isoformat(),
        window_end_utc=(NOW + timedelta(days=90)).isoformat(),
        confirmed_remaining_charge_units=1600,
        reserved_charge_units=0,
        max_charge_units_per_attempt=2,
        allocated_attempts=800,
        prior_reservations=[],
    )
    seed.update(changes)
    return seed


def ledger(tmp_path, clock, *, ready=True, **changes):
    path = tmp_path / "artificial.sqlite"
    seed_ledger(path, artificial_seed(**changes), now=clock.now)
    quota = AirLabsQuota(path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    quota.start()
    if ready:
        clock.advance(60)
    return quota


def contents(path):
    with sqlite3.connect(path) as connection:
        state = json.loads(connection.execute("SELECT body FROM state").fetchone()[0])
        rows = connection.execute("SELECT id, at_utc, charge_units FROM reservations").fetchall()
    return state, rows


def test_reservation_commits_and_restart_never_refunds_or_refills(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock, allocated_attempts=3)
    assert quota.reserve(clock.now) == 1
    state, rows = contents(quota.path)
    assert state["attempts_left"] == 2 and state["units_left"] == 4 and len(rows) == 1
    # Crash after reserve/before dispatch: the debit is already visible to another connection.
    quota.close()
    clock.advance(10)
    restarted = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    restarted.start()
    assert restarted.status(clock.now).reason == "cooldown"
    assert restarted.status(clock.now).retry_in_seconds == 60
    assert contents(quota.path)[0]["attempts_left"] == 2
    clock.advance(60)
    assert restarted.reserve(clock.now) == 2
    restarted.close()


def test_24_attempt_rolling_boundary_and_dedicated_budget_do_not_refill(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock, allocated_attempts=25)
    first = clock.now
    for _ in range(24):
        quota.reserve(clock.now)
        clock.advance(60)
    with pytest.raises(QuotaError, match="local_budget"):
        quota.reserve(clock.now)
    clock.advance((first + timedelta(days=1) - clock.now).total_seconds())
    quota.reserve(clock.now)  # Exactly t-24h is outside the rolling interval.
    clock.advance(32 * DAY)
    with pytest.raises(QuotaError, match="allocation_exhausted"):
        quota.reserve(clock.now)
    assert len(contents(quota.path)[1]) == 25
    quota.close()


def test_imported_history_enforces_800_without_resetting_at_month_change(tmp_path):
    clock = Clock()
    prior = [{"at_utc": (NOW - timedelta(days=2)).isoformat(), "charge_units": 2}] * 799
    quota = ledger(tmp_path, clock, prior_reservations=prior)
    quota.reserve(clock.now)
    clock.advance(60)
    with pytest.raises(QuotaError, match="local_budget"):
        quota.reserve(clock.now)
    assert len(contents(quota.path)[1]) == 800
    quota.close()


def test_smaller_allocation_and_known_charge_units_win(tmp_path):
    clock = Clock()
    quota = ledger(
        tmp_path,
        clock,
        allocated_attempts=1,
        confirmed_remaining_charge_units=5,
        reserved_charge_units=3,
    )
    quota.reserve(clock.now)
    clock.advance(60)
    with pytest.raises(QuotaError, match="allocation_exhausted"):
        quota.reserve(clock.now)
    assert contents(quota.path)[0]["units_left"] == 0
    quota.close()


def test_concurrent_calls_cannot_cross_shared_slot(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock)

    def reserve(_):
        try:
            return quota.reserve(clock.now)
        except QuotaError as error:
            return error.reason

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(32)))
    assert outcomes.count(1) == 1 and outcomes.count("cooldown") == 31
    assert len(contents(quota.path)[1]) == 1
    quota.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_uncertain_commit_always_denies_dispatch(tmp_path, monkeypatch, after_commit):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    original = quota._commit

    def broken_commit():
        if after_commit:
            original()
        raise sqlite3.OperationalError("ARTIFICIAL DISK FAILURE")

    monkeypatch.setattr(quota, "_commit", broken_commit)
    with pytest.raises(QuotaError, match="ledger_unavailable"):
        quota.reserve(clock.now)
    assert len(contents(quota.path)[1]) == int(after_commit)
    assert quota.status(clock.now).reason == "ledger_unavailable"
    quota.close()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: setattr(c, "now", c.now - timedelta(seconds=1)),
        lambda c: setattr(c, "now", c.now + timedelta(seconds=6)),
        lambda c: setattr(c, "now", c.now + timedelta(days=31)),
        lambda c: setattr(c, "mono", c.mono - 1),
        lambda c: setattr(c, "mono", float("nan")),
        lambda c: setattr(c, "mono", float("inf")),
    ],
)
def test_clock_discontinuities_latch_and_preserve_spend(tmp_path, mutation):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    quota.reserve(clock.now)
    mutation(clock)
    with pytest.raises(QuotaError, match="clock_untrusted"):
        quota.reserve(clock.now)
    state, rows = contents(quota.path)
    assert "clock_untrusted" in state["stops"] and len(rows) == 1
    quota.close()


def test_five_second_divergence_boundary_and_rollback_below_high_water(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    clock.now += timedelta(seconds=5)
    quota.reserve(clock.now)
    quota.close()
    clock.now -= timedelta(seconds=1)
    restarted = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    restarted.start()
    assert restarted.status(clock.now).reason == "clock_untrusted"
    restarted.close()


@pytest.mark.parametrize(
    ("delay", "indefinite"),
    [(60, False), (3600, False), (10**26, False), (10**400, True), (float("inf"), True)],
)
def test_retry_after_persists_and_cannot_be_shortened(tmp_path, delay, indefinite):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    quota.reserve(clock.now)
    quota.record_failure("rate_limited", clock.now, delay)
    until = contents(quota.path)[0]["blocked_until"]
    quota.record_failure("rate_limited", clock.now, 1)
    assert contents(quota.path)[0]["blocked_until"] == until
    quota.close()
    restarted = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    restarted.start()
    assert restarted.status(clock.now).reason == "rate_limited"
    if indefinite:
        assert until is None
        assert restarted.status(clock.now).retry_in_seconds is None
    else:
        assert restarted.status(clock.now).retry_in_seconds >= delay
    clock.advance(60)
    if indefinite or delay > 60:
        with pytest.raises(QuotaError, match="rate_limited"):
            restarted.reserve(clock.now)
        assert len(contents(quota.path)[1]) == 1
        assert contents(quota.path)[0]["attempts_left"] == 799
    restarted.close()


@pytest.mark.parametrize("reason", ["authentication", "entitlement", "month_exhausted"])
def test_reconciliation_only_clears_named_stop_and_preserves_counters(tmp_path, reason):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    quota.reserve(clock.now)
    quota.record_failure(reason, clock.now)
    quota.record_failure("clock_untrusted", clock.now)
    before = contents(quota.path)
    quota.close()
    reconcile_ledger(
        quota.path,
        dict(
            schema_version=1,
            verified_at_utc=clock.now.isoformat(),
            resolved_stops=["clock_untrusted"],
            allocation=None,
        ),
        now=clock.now,
    )
    after = contents(quota.path)
    assert after[1] == before[1] and after[0]["attempts_left"] == before[0]["attempts_left"]
    assert after[0]["stops"] == [reason]
    restarted = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    restarted.start()
    assert restarted.status(clock.now).reason == reason
    restarted.close()


def test_replacement_allocation_preserves_rolling_reservations(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock, allocated_attempts=1)
    quota.reserve(clock.now)
    rows = contents(quota.path)[1]
    quota.close()
    clock.advance(90 * DAY)
    replacement = artificial_seed(
        verified_at_utc=clock.now.isoformat(),
        window_start_utc=clock.now.isoformat(),
        window_end_utc=(clock.now + timedelta(days=30)).isoformat(),
        allocated_attempts=2,
    )
    reconcile_ledger(
        quota.path,
        dict(
            schema_version=1,
            verified_at_utc=clock.now.isoformat(),
            resolved_stops=["allocation_expired"],
            allocation=replacement,
        ),
        now=clock.now,
    )
    assert contents(quota.path)[1] == rows
    assert contents(quota.path)[0]["attempts_left"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"allocated_attempts": 801},
        {"allocated_attempts": True},
        {"confirmed_remaining_charge_units": None},
        {"max_charge_units_per_attempt": 0},
        {"max_charge_units_per_attempt": "1"},
        {"reserved_charge_units": 1601},
        {"prior_reservations": None},
        {"window_end_utc": NOW.isoformat()},
        {"verified_at_utc": (NOW + timedelta(seconds=1)).isoformat()},
        {"account_identifier": "forbidden"},
    ],
)
def test_unknown_or_invalid_seed_never_creates_ledger(tmp_path, changes):
    path = tmp_path / "invalid.sqlite"
    with pytest.raises(QuotaError):
        seed_ledger(path, artificial_seed(**changes), now=NOW)
    assert not path.exists()


def test_existing_ledger_second_owner_and_sqlite_writer_lock_fail_closed(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    before = quota.path.read_bytes()
    with pytest.raises(QuotaError):
        seed_ledger(quota.path, artificial_seed(), now=NOW)
    assert quota.path.read_bytes() == before
    second = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    with pytest.raises(QuotaError):
        second.start()
    with sqlite3.connect(quota.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(QuotaError):
            quota.reserve(clock.now)
    assert contents(quota.path)[1] == []
    quota.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "version", "readonly", "state"])
def test_damaged_accounting_is_not_recreated(tmp_path, damage):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    quota.close()
    if damage == "missing":
        quota.path.unlink()
    elif damage == "corrupt":
        quota.path.write_bytes(b"artificial broken sqlite")
    elif damage == "readonly":
        quota.path.chmod(0o400)
    else:
        with sqlite3.connect(quota.path) as connection:
            connection.execute(
                "PRAGMA user_version=2" if damage == "version" else "UPDATE state SET body='{}'"
            )
    before = quota.path.read_bytes() if quota.path.exists() else None
    restarted = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    with pytest.raises(QuotaError):
        restarted.start()
    assert (quota.path.read_bytes() if quota.path.exists() else None) == before


def test_expired_restart_is_suspended_and_no_sensitive_columns_exist(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock, window_end_utc=(NOW + timedelta(seconds=120)).isoformat())
    quota.reserve(clock.now)
    quota.close()
    clock.advance(60)
    restarted = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    restarted.start()
    assert restarted.status(clock.now).reason == "allocation_expired"
    with sqlite3.connect(quota.path) as connection:
        dump = "\n".join(connection.iterdump())
    assert all(word not in dump for word in ("api_key", "icao24", "callsign", "airport", "http"))
    restarted.close()


def test_small_observed_wall_rollback_without_a_reservation_still_latches(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    assert quota.clock_valid(clock.now)
    clock.now -= timedelta(seconds=1)
    assert not quota.clock_valid(clock.now)
    with pytest.raises(QuotaError, match="clock_untrusted"):
        quota.reserve(clock.now)
    assert contents(quota.path)[1] == []
    quota.close()


def test_second_process_cannot_acquire_lifespan_lock(tmp_path):
    import subprocess
    import sys

    clock = Clock()
    quota = ledger(tmp_path, clock)
    script = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(quota.path) + ".lock"], check=False)
    assert result.returncode == 0
    quota.close()


def test_missing_posix_locking_never_creates_ledger(tmp_path, monkeypatch):
    import skytrace.services.airlabs_quota as module

    monkeypatch.setattr(module, "fcntl", None)
    with pytest.raises(QuotaError):
        seed_ledger(tmp_path / "unsupported.sqlite", artificial_seed(), now=NOW)
    assert not list(tmp_path.iterdir())


def test_duplicate_accounting_fields_fail_closed(tmp_path):
    clock = Clock()
    quota = ledger(tmp_path, clock)
    quota.close()
    with sqlite3.connect(quota.path) as connection:
        body = connection.execute("SELECT body FROM state").fetchone()[0]
        connection.execute("UPDATE state SET body=?", ('{"version":1,' + body[1:],))
    restarted = AirLabsQuota(quota.path, clock=lambda: clock.now, monotonic=lambda: clock.mono)
    with pytest.raises(QuotaError):
        restarted.start()
