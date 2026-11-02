"""Local, single-owner SQLite accounting. No credentials or aircraft data belong here."""

import argparse
import json
import math
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from skytrace.services.airlabs_models import ContextReason, utc_timestamp

try:
    import fcntl
except ImportError:  # An unsupported locking platform must not break off-mode imports.
    fcntl = None

DAY = 86400
MONTH = 31 * DAY
STOP_REASONS = {
    "authentication",
    "entitlement",
    "month_exhausted",
    "clock_untrusted",
    "allocation_expired",
    "ledger_unavailable",
}


@dataclass(frozen=True)
class QuotaStatus:
    reason: ContextReason | None = None
    retry_in_seconds: float | None = None


class QuotaError(Exception):
    def __init__(self, reason: ContextReason, retry_in_seconds: float | None = None) -> None:
        self.reason = reason
        self.retry_in_seconds = retry_in_seconds
        super().__init__(reason)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate accounting field")
        value[key] = item
    return value


def _timestamp(value) -> float:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Invalid UTC timestamp")
    return utc_timestamp(value).timestamp()


def _integer(value, minimum=0) -> int:
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise ValueError("Invalid accounting integer")
    return value


def _number(value) -> bool:
    return (
        type(value) in (int, float) and value >= 0 and (type(value) is int or math.isfinite(value))
    )


def _delay(until, now: float) -> float | None:
    if until is None:
        return None
    try:
        result = float(max(0, until - now))
        return result if math.isfinite(result) else None
    except OverflowError:
        return None


def _seed_state(seed: dict, now: float) -> tuple[dict, list[tuple[float, int]]]:
    expected = {
        "schema_version",
        "verified_at_utc",
        "window_start_utc",
        "window_end_utc",
        "confirmed_remaining_charge_units",
        "reserved_charge_units",
        "max_charge_units_per_attempt",
        "allocated_attempts",
        "prior_reservations",
    }
    if not isinstance(seed, dict) or set(seed) != expected:
        raise ValueError("Invalid seed schema")
    if type(seed["schema_version"]) is not int or seed["schema_version"] != 1:
        raise ValueError("Invalid schema version")
    verified, start, end = (
        _timestamp(seed[key]) for key in ("verified_at_utc", "window_start_utc", "window_end_utc")
    )
    if not start <= verified <= now < end:
        raise ValueError("Unverified or expired allocation")
    remaining = _integer(seed["confirmed_remaining_charge_units"])
    reserved = _integer(seed["reserved_charge_units"])
    charge = _integer(seed["max_charge_units_per_attempt"], 1)
    attempts = _integer(seed["allocated_attempts"], 1)
    if not attempts <= 800 or reserved > remaining or attempts > (remaining - reserved) // charge:
        raise ValueError("Insufficient dedicated allocation")
    if not isinstance(seed["prior_reservations"], list):
        raise ValueError("Prior accounting must be explicitly known")
    prior = []
    for item in seed["prior_reservations"]:
        if not isinstance(item, dict) or set(item) != {"at_utc", "charge_units"}:
            raise ValueError("Invalid prior reservation")
        at = _timestamp(item["at_utc"])
        if not verified - MONTH < at <= verified:
            raise ValueError("Prior reservation outside verified window")
        prior.append((at, _integer(item["charge_units"], 1)))
    return dict(
        version=1,
        verified_at=verified,
        window_start=start,
        window_end=end,
        allocated_attempts=attempts,
        attempts_left=attempts,
        allocated_units=attempts * charge,
        units_left=attempts * charge,
        charge=charge,
        high_water=now,
        next_slot_at=now + 60,
        blocked_until=0,
        stops=[],
    ), prior


def _validate_state(state: dict) -> None:
    expected = {
        "version",
        "verified_at",
        "window_start",
        "window_end",
        "allocated_attempts",
        "attempts_left",
        "allocated_units",
        "units_left",
        "charge",
        "high_water",
        "next_slot_at",
        "blocked_until",
        "stops",
    }
    if not isinstance(state, dict) or set(state) != expected:
        raise ValueError("Invalid ledger state")
    if type(state["version"]) is not int or state["version"] != 1:
        raise ValueError("Invalid ledger version")
    for key in ("allocated_attempts", "attempts_left", "allocated_units", "units_left", "charge"):
        _integer(state[key], 1 if key in {"allocated_attempts", "charge"} else 0)
    for key in ("verified_at", "window_start", "window_end", "high_water", "next_slot_at"):
        if not _number(state[key]):
            raise ValueError("Invalid ledger time")
    if state["blocked_until"] is not None and not _number(state["blocked_until"]):
        raise ValueError("Invalid cooldown")
    if not (
        state["window_start"] <= state["verified_at"] <= state["high_water"]
        and state["verified_at"] < state["window_end"]
        and state["attempts_left"] <= state["allocated_attempts"] <= 800
        and state["allocated_units"] == state["allocated_attempts"] * state["charge"]
        and state["units_left"] == state["attempts_left"] * state["charge"]
    ):
        raise ValueError("Inconsistent ledger state")
    if (
        not isinstance(state["stops"], list)
        or any(type(reason) is not str or reason not in STOP_REASONS for reason in state["stops"])
        or len(set(state["stops"])) != len(state["stops"])
    ):
        raise ValueError("Invalid ledger stops")


def _lock(path: Path):
    if fcntl is None:
        raise ValueError("Local advisory locking is unavailable")
    if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
        raise ValueError("Invalid ledger path")
    if path.parent.stat().st_mode & 0o022:
        raise ValueError("Unprotected ledger directory")
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Invalid lock file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink() or not path.stat().st_mode & 0o222:
        raise ValueError("Missing or read-only ledger")
    if path.stat().st_mode & 0o077:
        raise ValueError("Unprotected ledger")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=rw",
        uri=True,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ValueError("Durable local journaling unavailable")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Invalid ledger integrity")
        return connection
    except BaseException:
        connection.close()
        raise


def _load(connection: sqlite3.Connection) -> tuple[dict, list[tuple[float, int]]]:
    if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ValueError("Incompatible ledger")
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    if tables != [("reservations",), ("state",)]:
        raise ValueError("Invalid ledger tables")
    records = connection.execute("SELECT id, body FROM state").fetchall()
    if len(records) != 1 or records[0][0] != 1:
        raise ValueError("Invalid ledger singleton")
    state = json.loads(records[0][1], object_pairs_hook=_unique_object)
    _validate_state(state)
    rows = connection.execute(
        "SELECT at_utc, charge_units FROM reservations ORDER BY id"
    ).fetchall()
    for at, units in rows:
        if not _number(at) or at > state["high_water"]:
            raise ValueError("Invalid reservation time")
        _integer(units, 1)
    return state, rows


def _save(connection: sqlite3.Connection, state: dict) -> None:
    _validate_state(state)
    connection.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state),))


class AirLabsQuota:
    """Lifetime lock plus serialized, durable reservation transactions."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.clock = clock
        self.monotonic = monotonic
        self._mutex = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._fd: int | None = None
        self._state: dict | None = None
        self._reservations: list[tuple[float, int]] = []
        self._failed = False
        self._clock_fault = False
        self._wall_anchor = self._mono_anchor = self._last_mono = self._last_wall = 0.0

    def start(self) -> None:
        if self._connection is not None or self._failed:
            raise QuotaError("ledger_unavailable")
        try:
            self._fd = _lock(self.path)
            self._connection = _connect(self.path)
            self._connection.execute("BEGIN IMMEDIATE")
            state, reservations = _load(self._connection)
            now = _timestamp(self.clock())
            mono = self.monotonic()
            if not math.isfinite(mono):
                raise ValueError("Invalid monotonic clock")
            if now < state["high_water"]:
                self._add_stop(state, "clock_untrusted")
            if now >= state["window_end"]:
                self._add_stop(state, "allocation_expired")
            state["high_water"] = max(state["high_water"], now)
            state["next_slot_at"] = max(state["next_slot_at"], now + 60)
            _save(self._connection, state)
            self._commit()
            self._state, self._reservations = state, reservations
            self._wall_anchor, self._mono_anchor, self._last_mono = now, mono, mono
            self._last_wall = now
        except Exception:
            self._failed = True
            self.close()
            raise QuotaError("ledger_unavailable") from None

    def close(self) -> None:
        with self._mutex:
            try:
                if self._connection is not None:
                    self._connection.close()
            finally:
                self._connection = None
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None

    def _commit(self) -> None:
        self._connection.execute("COMMIT")

    @staticmethod
    def _add_stop(state: dict, reason: str) -> None:
        if reason not in state["stops"]:
            state["stops"].append(reason)

    def clock_valid(self, now: datetime) -> bool:
        if self._state is None:
            return False
        try:
            wall, mono = _timestamp(now), self.monotonic()
            elapsed = mono - self._mono_anchor
            valid = (
                math.isfinite(elapsed)
                and elapsed >= 0
                and mono >= self._last_mono
                and wall >= self._last_wall
                and wall >= self._state["high_water"]
                and abs((wall - self._wall_anchor) - elapsed) <= 5
            )
            self._last_mono = mono
            self._last_wall = wall
            self._clock_fault |= not valid
        except (ValueError, TypeError, OverflowError):
            self._clock_fault = True
        return not self._clock_fault

    def status(self, now: datetime) -> QuotaStatus:
        """Cached projection only: no SQLite or provider I/O and no reservation."""
        state = self._state
        if self._failed or self._connection is None or state is None:
            return QuotaStatus("ledger_unavailable")
        if self._clock_fault:
            return QuotaStatus("clock_untrusted")
        if state["stops"]:
            return QuotaStatus(state["stops"][0])
        timestamp = _timestamp(now)
        if not state["window_start"] <= timestamp < state["window_end"]:
            return QuotaStatus("allocation_expired")
        if state["attempts_left"] == 0 or state["units_left"] < state["charge"]:
            return QuotaStatus("allocation_exhausted")
        for window, limit in ((DAY, 24), (MONTH, 800)):
            times = sorted(at for at, _ in self._reservations if timestamp - window < at)
            if len(times) >= limit:
                return QuotaStatus("local_budget", _delay(times[-limit] + window, timestamp))
        blocked = state["blocked_until"]
        if blocked is None or timestamp < blocked:
            return QuotaStatus("rate_limited", _delay(blocked, timestamp))
        if timestamp < state["next_slot_at"]:
            return QuotaStatus("cooldown", _delay(state["next_slot_at"], timestamp))
        return QuotaStatus()

    def reserve(self, now: datetime) -> int:
        """Commit a debit before returning the reservation ID. Never refund it."""
        return self._transaction(now, reserve=True)

    def record_failure(
        self,
        reason: ContextReason,
        now: datetime,
        retry_after: int | float | None = None,
    ) -> None:
        self._transaction(now, failure=reason, retry_after=retry_after)

    def _transaction(self, now, *, reserve=False, failure=None, retry_after=None):
        with self._mutex:
            if self._failed or self._connection is None:
                raise QuotaError("ledger_unavailable")
            decision = QuotaStatus()
            reservation = None
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                state, rows = _load(self._connection)
                self._state, self._reservations = state, rows
                timestamp = _timestamp(now)
                if not self.clock_valid(now):
                    self._add_stop(state, "clock_untrusted")
                if timestamp >= state["window_end"]:
                    self._add_stop(state, "allocation_expired")
                if failure in STOP_REASONS:
                    self._add_stop(state, failure)
                if failure == "rate_limited":
                    if (
                        retry_after is not None
                        and type(retry_after) is float
                        and not math.isfinite(retry_after)
                    ):
                        state["blocked_until"] = None
                    elif state["blocked_until"] is not None:
                        delay = retry_after if _number(retry_after) else 60
                        try:
                            deadline = timestamp + max(60, delay)
                        except OverflowError:
                            deadline = math.inf
                        # Persist an indefinite cooldown rather than roll back and
                        # lose an unrepresentable provider deadline on restart.
                        state["blocked_until"] = (
                            max(state["blocked_until"], deadline)
                            if math.isfinite(deadline)
                            else None
                        )
                state["high_water"] = max(state["high_water"], timestamp)
                if reserve:
                    decision = self.status(now)
                    if decision.reason is None:
                        cursor = self._connection.execute(
                            "INSERT INTO reservations(at_utc, charge_units) VALUES (?, ?)",
                            (timestamp, state["charge"]),
                        )
                        reservation = cursor.lastrowid
                        state["attempts_left"] -= 1
                        state["units_left"] -= state["charge"]
                        state["next_slot_at"] = timestamp + 60
                _save(self._connection, state)
                self._commit()
                if reservation is not None:
                    self._reservations.append((timestamp, state["charge"]))
            except Exception:
                self._failed = True
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
                raise QuotaError("ledger_unavailable") from None
            if decision.reason is not None:
                raise QuotaError(decision.reason, decision.retry_in_seconds)
            return reservation


def seed_ledger(path: Path, seed: dict, *, now: datetime) -> None:
    """Explicit offline operation; refuses to replace even a corrupt existing file."""
    fd = connection = None
    try:
        state, prior = _seed_state(seed, _timestamp(now))
        fd = _lock(path)
        created = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(created)
        connection = _connect(path)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE reservations (id INTEGER PRIMARY KEY, at_utc REAL NOT NULL, "
            "charge_units INTEGER NOT NULL CHECK(charge_units>0))"
        )
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO state VALUES (1, ?)", (json.dumps(state),))
        connection.executemany(
            "INSERT INTO reservations(at_utc, charge_units) VALUES (?, ?)", prior
        )
        connection.execute("COMMIT")
        # Sync the directory entry as well as SQLite's full-synchronous transaction.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        raise QuotaError("ledger_unavailable") from None
    finally:
        if connection is not None:
            connection.close()
        if fd is not None:
            os.close(fd)


def reconcile_ledger(path: Path, reconciliation: dict, *, now: datetime) -> None:
    """Explicitly resolve named stops; never alter or delete rolling reservations.

    A replacement allocation uses the seed schema but must have an empty prior list:
    the existing ledger remains authoritative for all earlier reservations.
    """
    fd = connection = None
    try:
        if set(reconciliation) != {
            "schema_version",
            "verified_at_utc",
            "resolved_stops",
            "allocation",
        }:
            raise ValueError("Invalid reconciliation")
        if (
            type(reconciliation["schema_version"]) is not int
            or reconciliation["schema_version"] != 1
        ):
            raise ValueError("Invalid reconciliation version")
        verified, timestamp = _timestamp(reconciliation["verified_at_utc"]), _timestamp(now)
        if verified > timestamp:
            raise ValueError("Future reconciliation verification")
        resolved = reconciliation["resolved_stops"]
        if not isinstance(resolved, list) or any(reason not in STOP_REASONS for reason in resolved):
            raise ValueError("Invalid resolved stops")
        fd = _lock(path)
        connection = _connect(path)
        connection.execute("BEGIN IMMEDIATE")
        state, rows = _load(connection)
        if any(at > timestamp for at, _ in rows):
            raise ValueError("Clock correction cannot move reservations")
        if timestamp < state["high_water"] and "clock_untrusted" not in resolved:
            raise ValueError("Unresolved clock correction")
        replacement = reconciliation["allocation"]
        if replacement is not None:
            new_state, prior = _seed_state(replacement, timestamp)
            if prior:
                raise ValueError("Existing reservations must be preserved, not reimported")
            for key in (
                "verified_at",
                "window_start",
                "window_end",
                "allocated_attempts",
                "attempts_left",
                "allocated_units",
                "units_left",
                "charge",
            ):
                state[key] = new_state[key]
        if not state["window_start"] <= timestamp < state["window_end"]:
            raise ValueError("Unresolved allocation window")
        state["stops"] = [reason for reason in state["stops"] if reason not in resolved]
        state["high_water"] = timestamp
        state["next_slot_at"] = max(state["next_slot_at"], timestamp + 60)
        _save(connection, state)
        connection.execute("COMMIT")
    except Exception:
        raise QuotaError("ledger_unavailable") from None
    finally:
        if connection is not None:
            connection.close()
        if fd is not None:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline AirLabs accounting; no source requests")
    parser.add_argument("operation", choices=("seed", "reconcile"))
    parser.add_argument("accounting_json", type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    try:
        with args.accounting_json.open("rb") as source:
            content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Accounting input too large")
        payload = json.loads(content, object_pairs_hook=_unique_object)
        operation = seed_ledger if args.operation == "seed" else reconcile_ledger
        operation(args.ledger, payload, now=datetime.now(UTC))
    except Exception:
        print("ledger_unavailable")
        return 1
    print("Accounting updated offline; live activation remains separately gated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
