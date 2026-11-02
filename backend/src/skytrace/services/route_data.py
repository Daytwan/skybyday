"""Read-only, indexed VRS standing-route data for local fleet resolution."""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ASSET_PATH = Path(__file__).parents[1] / "assets" / "vrs-standing-routes.sqlite3"
AirportRecord = tuple[str, str | None, str | None, float | None, float | None]


@dataclass
class RouteReferenceIndex:
    """SQLite-backed immutable index with a bounded airport page cache."""

    connection: sqlite3.Connection | None
    source_url: str
    source_commit: str
    retrieved_at: datetime
    airport_cache: OrderedDict[str, AirportRecord]
    airport_cache_limit: int = 256

    @classmethod
    def empty(cls) -> RouteReferenceIndex:
        return cls(
            None,
            "https://github.com/vradarserver/standing-data",
            "",
            datetime.fromtimestamp(0, UTC),
            OrderedDict(),
        )

    @classmethod
    def from_path(cls, path: Path) -> RouteReferenceIndex:
        """Open an immutable SQLite asset and validate its source metadata."""

        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            source_url = json.loads(metadata["url"])
            source_commit = json.loads(metadata["commit"])
            retrieved_at = datetime.fromisoformat(
                json.loads(metadata["retrieved_at"]).replace("Z", "+00:00")
            )
            if (
                not isinstance(source_url, str)
                or not source_url
                or not isinstance(source_commit, str)
                or not source_commit
                or retrieved_at.tzinfo is None
                or retrieved_at.utcoffset() != UTC.utcoffset(retrieved_at)
                or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            ):
                raise ValueError("Invalid VRS route-index metadata")
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError, json.JSONDecodeError):
            connection.close()
            raise ValueError("Invalid VRS route-index") from None
        return cls(connection, source_url, source_commit, retrieved_at, OrderedDict())

    @classmethod
    def bundled(cls) -> RouteReferenceIndex:
        try:
            return cls.from_path(ASSET_PATH)
        except (OSError, ValueError, sqlite3.DatabaseError):
            return cls.empty()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.airport_cache.clear()

    def lookup(self, callsign: str) -> tuple[str, ...] | None:
        if self.connection is None:
            return None
        row = self.connection.execute(
            "SELECT airport_codes FROM routes WHERE callsign = ?", (callsign,)
        ).fetchone()
        if row is None:
            return None
        values = json.loads(row[0])
        if not isinstance(values, list) or not 2 <= len(values) <= 12 or not all(
            isinstance(value, str) and 3 <= len(value) <= 8 and value.isalnum()
            for value in values
        ):
            raise ValueError("Invalid VRS route-index row")
        return tuple(values)

    def airport(self, code: str) -> AirportRecord | None:
        cached = self.airport_cache.get(code)
        if cached is not None:
            self.airport_cache.move_to_end(code)
            return cached
        if self.connection is None:
            return None
        row = self.connection.execute(
            "SELECT name, icao, iata, latitude, longitude FROM airports WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            return None
        name, icao, iata, latitude, longitude = row
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 200
            or (icao is not None and (not isinstance(icao, str) or not 3 <= len(icao) <= 8))
            or (iata is not None and (not isinstance(iata, str) or not 3 <= len(iata) <= 4))
            or (latitude is None) != (longitude is None)
            or (latitude is not None and not -90 <= float(latitude) <= 90)
            or (longitude is not None and not -180 <= float(longitude) <= 180)
        ):
            raise ValueError("Invalid VRS airport-index row")
        record: AirportRecord = (name, icao, iata, latitude, longitude)
        self.airport_cache[code] = record
        if len(self.airport_cache) > self.airport_cache_limit:
            self.airport_cache.popitem(last=False)
        return record
