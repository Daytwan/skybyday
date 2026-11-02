"""Optional bounded PostGIS trajectory persistence and querying."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module, resources
from typing import Any, Literal

from pydantic import AwareDatetime, Field

from skytrace.config import Settings
from skytrace.models import (
    AircraftObservation,
    FrozenModel,
    ProviderBatch,
    SourceAttribution,
    SourceName,
)

logger = logging.getLogger(__name__)

QUEUE_BATCHES = 8
MAX_BATCH_OBSERVATIONS = 10_000
DATABASE_TIMEOUT_SECONDS = 5.0
SHUTDOWN_DRAIN_SECONDS = 3.0
RETRY_DELAYS_SECONDS = (5.0, 10.0, 20.0)
RETENTION_CHUNK = 10_000


class HistoryUnavailable(RuntimeError):
    """The optional history service cannot currently serve the request."""


class HistoryStatus(FrozenModel):
    enabled: bool
    state: Literal["disabled", "starting", "available", "degraded", "stopped"]
    queue_capacity_batches: Literal[8] = QUEUE_BATCHES
    queued_batches: int = Field(ge=0)
    accepted_batches: int = Field(ge=0)
    accepted_observations: int = Field(ge=0)
    written_observations: int = Field(ge=0)
    duplicate_observations: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    dropped_queue_batches: int = Field(ge=0)
    dropped_queue_observations: int = Field(ge=0)
    dropped_write_observations: int = Field(ge=0)
    dropped_shutdown_observations: int = Field(ge=0)
    last_write_at: AwareDatetime | None
    last_error: str | None


class AircraftHistoryResponse(FrozenModel):
    schema_version: Literal[1] = 1
    source: SourceName
    icao24: str
    start: AwareDatetime
    end: AwareDatetime
    truncated: bool
    observations: tuple[AircraftObservation, ...]
    attribution: SourceAttribution | None


class PositionHistoryResponse(FrozenModel):
    schema_version: Literal[1] = 1
    source: SourceName
    start: AwareDatetime
    end: AwareDatetime
    truncated: bool
    observations: tuple[AircraftObservation, ...]
    attribution: SourceAttribution | None


@dataclass(frozen=True, slots=True)
class _WriteBatch:
    observations: tuple[AircraftObservation, ...]
    attribution: SourceAttribution


def _driver() -> tuple[Any, Any, Any]:
    try:
        psycopg = import_module("psycopg")
        sql = import_module("psycopg.sql")
        dict_row = import_module("psycopg.rows").dict_row
    except ImportError as error:
        raise HistoryUnavailable("PostgreSQL history support is not installed") from error
    return psycopg, sql, dict_row


def migration_sql(schema: str) -> Any:
    """Return the packaged migration safely composed with the validated schema name."""
    _, sql, _ = _driver()
    template = (
        resources.files("skytrace")
        .joinpath("migrations/001_history.sql")
        .read_text(encoding="utf-8")
    )
    before, after = template.split("__SCHEMA__", maxsplit=1)
    pieces: list[Any] = [sql.SQL(before), sql.Identifier(schema)]
    while "__SCHEMA__" in after:
        before, after = after.split("__SCHEMA__", maxsplit=1)
        pieces.extend((sql.SQL(before), sql.Identifier(schema)))
    pieces.append(sql.SQL(after))
    return sql.Composed(pieces)


async def migrate(settings: Settings) -> None:
    if settings.history_database_url is None:
        raise HistoryUnavailable("History database URL is not configured")
    psycopg, _, _ = _driver()
    dsn = settings.history_database_url.get_secret_value()
    try:
        async with asyncio.timeout(DATABASE_TIMEOUT_SECONDS):
            connection = await psycopg.AsyncConnection.connect(
                dsn, connect_timeout=int(DATABASE_TIMEOUT_SECONDS), autocommit=True
            )
            async with connection:
                await connection.execute(migration_sql(settings.history_schema), prepare=False)
    except HistoryUnavailable:
        raise
    except Exception as error:
        raise HistoryUnavailable("History migration failed") from error


class HistoryStore:
    """One non-blocking producer queue, one writer, and bounded query connections."""

    def __init__(self, settings: Settings, *, sleep: Any = asyncio.sleep) -> None:
        self.settings = settings
        self.enabled = settings.history_enabled
        self._dsn = (
            settings.history_database_url.get_secret_value()
            if settings.history_database_url is not None
            else None
        )
        self._schema = settings.history_schema
        self._retention = timedelta(days=settings.history_retention_days)
        self._sleep = sleep
        self._queue: asyncio.Queue[_WriteBatch] = asyncio.Queue(maxsize=QUEUE_BATCHES)
        self._query_slots = asyncio.Semaphore(2)
        self._task: asyncio.Task[None] | None = None
        self._accepting = False
        self._state: Literal["disabled", "starting", "available", "degraded", "stopped"] = (
            "disabled" if not self.enabled else "starting"
        )
        self._accepted_batches = 0
        self._accepted_observations = 0
        self._written_observations = 0
        self._duplicate_observations = 0
        self._retry_count = 0
        self._failure_count = 0
        self._dropped_queue_batches = 0
        self._dropped_queue_observations = 0
        self._dropped_write_observations = 0
        self._dropped_shutdown_observations = 0
        self._inflight_observations = 0
        self._last_write_at: datetime | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None:
            raise RuntimeError("A history store can only be started once")
        self._accepting = True
        self._task = asyncio.create_task(self._run(), name="skytrace-history-writer")

    def offer(self, batch: ProviderBatch) -> bool:
        """Offer one fresh provider batch without waiting on storage."""
        if not self._accepting:
            return False
        positioned = tuple(
            observation
            for observation in batch.aircraft[:MAX_BATCH_OBSERVATIONS]
            if observation.position_observed_at is not None
        )
        if not positioned:
            return True
        item = _WriteBatch(positioned, batch.source)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped_queue_batches += 1
            self._dropped_queue_observations += len(positioned)
            return False
        self._accepted_batches += 1
        self._accepted_observations += len(positioned)
        return True

    def status(self) -> HistoryStatus:
        return HistoryStatus(
            enabled=self.enabled,
            state=self._state,
            queued_batches=self._queue.qsize(),
            accepted_batches=self._accepted_batches,
            accepted_observations=self._accepted_observations,
            written_observations=self._written_observations,
            duplicate_observations=self._duplicate_observations,
            retry_count=self._retry_count,
            failure_count=self._failure_count,
            dropped_queue_batches=self._dropped_queue_batches,
            dropped_queue_observations=self._dropped_queue_observations,
            dropped_write_observations=self._dropped_write_observations,
            dropped_shutdown_observations=self._dropped_shutdown_observations,
            last_write_at=self._last_write_at,
            last_error=self._last_error,
        )

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            self._inflight_observations = len(item.observations)
            try:
                await self._write_with_retry(item)
            finally:
                self._inflight_observations = 0
                self._queue.task_done()

    async def _write_with_retry(self, item: _WriteBatch) -> None:
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                inserted = await self._insert(item)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failure_count += 1
                self._state = "degraded"
                self._last_error = f"Database operation failed ({type(error).__name__})"
                if attempt == len(RETRY_DELAYS_SECONDS):
                    self._dropped_write_observations += len(item.observations)
                    logger.warning("Dropping history batch after bounded retries")
                    return
                self._retry_count += 1
                await self._sleep(RETRY_DELAYS_SECONDS[attempt])
                continue
            self._written_observations += inserted
            self._duplicate_observations += len(item.observations) - inserted
            self._last_write_at = datetime.now(UTC)
            self._last_error = None
            self._state = "available"
            await self._maybe_retain()
            return

    async def _connection(self, *, row_factory: Any = None) -> Any:
        if not self.enabled or self._dsn is None:
            raise HistoryUnavailable("History is disabled")
        psycopg, _, _ = _driver()
        kwargs = {"connect_timeout": int(DATABASE_TIMEOUT_SECONDS)}
        if row_factory is not None:
            kwargs["row_factory"] = row_factory
        return await psycopg.AsyncConnection.connect(self._dsn, **kwargs)

    async def _insert(self, item: _WriteBatch) -> int:
        _, sql, _ = _driver()
        query = sql.SQL(
            """INSERT INTO {}.aircraft_positions
            (source, icao24, position_observed_at, provider_timestamp, received_at,
             latitude_deg, longitude_deg, geom, observation, attribution)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s::jsonb, %s::jsonb)
            ON CONFLICT (source, icao24, position_observed_at) DO NOTHING"""
        ).format(sql.Identifier(self._schema))
        rows = [
            (
                observation.source,
                observation.icao24,
                observation.position_observed_at,
                observation.provider_timestamp,
                observation.received_at,
                observation.latitude_deg,
                observation.longitude_deg,
                observation.longitude_deg,
                observation.latitude_deg,
                observation.model_dump_json(),
                item.attribution.model_dump_json(),
            )
            for observation in item.observations
        ]
        async with asyncio.timeout(DATABASE_TIMEOUT_SECONDS):
            connection = await self._connection()
            async with connection:
                async with connection.cursor() as cursor:
                    await cursor.executemany(query, rows)
                    inserted = cursor.rowcount
                await connection.commit()
        return inserted

    async def _maybe_retain(self) -> None:
        now = datetime.now(UTC)
        # One bounded deletion per committed batch can keep up with even the
        # largest accepted batch; idle databases do not grow between writes.
        _, sql, _ = _driver()
        query = sql.SQL(
            """DELETE FROM {}.aircraft_positions WHERE ctid IN (
                SELECT ctid FROM {}.aircraft_positions
                WHERE position_observed_at < %s ORDER BY position_observed_at LIMIT %s
            )"""
        ).format(sql.Identifier(self._schema), sql.Identifier(self._schema))
        try:
            async with asyncio.timeout(DATABASE_TIMEOUT_SECONDS):
                connection = await self._connection()
                async with connection:
                    await connection.execute(query, (now - self._retention, RETENTION_CHUNK))
                    await connection.commit()
        except Exception as error:
            self._failure_count += 1
            self._state = "degraded"
            self._last_error = f"Retention failed ({type(error).__name__})"

    async def _query(self, query: Any, parameters: tuple[Any, ...], limit: int) -> tuple[Any, ...]:
        if not self.enabled:
            raise HistoryUnavailable("History is disabled")
        _, _, dict_row = _driver()
        try:
            async with asyncio.timeout(DATABASE_TIMEOUT_SECONDS):
                async with self._query_slots:
                    connection = await self._connection(row_factory=dict_row)
                    async with connection:
                        result = await connection.execute(query, parameters + (limit + 1,))
                        rows = await result.fetchall()
        except HistoryUnavailable:
            raise
        except Exception as error:
            self._failure_count += 1
            self._state = "degraded"
            self._last_error = f"Database query failed ({type(error).__name__})"
            raise HistoryUnavailable("History is temporarily unavailable") from error
        self._state = "available"
        self._last_error = None
        return tuple(rows)

    @staticmethod
    def _decode_rows(
        rows: tuple[Any, ...], limit: int
    ) -> tuple[tuple[AircraftObservation, ...], SourceAttribution | None, bool]:
        selected = rows[:limit]
        observations = tuple(
            AircraftObservation.model_validate_json(row["observation"]) for row in selected
        )
        attribution = (
            SourceAttribution.model_validate_json(selected[0]["attribution"]) if selected else None
        )
        return observations, attribution, len(rows) > limit

    async def aircraft(
        self, source: SourceName, icao24: str, start: datetime, end: datetime, limit: int
    ) -> AircraftHistoryResponse:
        _, sql, _ = _driver()
        query = sql.SQL(
            """SELECT observation::text AS observation, attribution::text AS attribution
            FROM {}.aircraft_positions
            WHERE source = %s AND icao24 = %s
              AND position_observed_at >= %s AND position_observed_at < %s
            ORDER BY position_observed_at ASC LIMIT %s"""
        ).format(sql.Identifier(self._schema))
        rows = await self._query(query, (source, icao24, start, end), limit)
        observations, attribution, truncated = self._decode_rows(rows, limit)
        return AircraftHistoryResponse(
            source=source,
            icao24=icao24,
            start=start,
            end=end,
            truncated=truncated,
            observations=observations,
            attribution=attribution,
        )

    async def positions(
        self,
        source: SourceName,
        start: datetime,
        end: datetime,
        latitude_deg: float,
        longitude_deg: float,
        radius_m: float,
        limit: int,
    ) -> PositionHistoryResponse:
        _, sql, _ = _driver()
        query = sql.SQL(
            """SELECT observation::text AS observation, attribution::text AS attribution
            FROM {}.aircraft_positions
            WHERE source = %s AND position_observed_at >= %s AND position_observed_at < %s
              AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY position_observed_at ASC, icao24 ASC LIMIT %s"""
        ).format(sql.Identifier(self._schema))
        rows = await self._query(
            query, (source, start, end, longitude_deg, latitude_deg, radius_m), limit
        )
        observations, attribution, truncated = self._decode_rows(rows, limit)
        return PositionHistoryResponse(
            source=source,
            start=start,
            end=end,
            truncated=truncated,
            observations=observations,
            attribution=attribution,
        )

    async def stop(self) -> None:
        if not self.enabled:
            return
        self._accepting = False
        if self._task is None:
            self._state = "stopped"
            return
        try:
            async with asyncio.timeout(SHUTDOWN_DRAIN_SECONDS):
                await self._queue.join()
        except TimeoutError:
            self._dropped_shutdown_observations += self._inflight_observations
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            while not self._queue.empty():
                item = self._queue.get_nowait()
                self._dropped_shutdown_observations += len(item.observations)
                self._queue.task_done()
        else:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._state = "stopped"
