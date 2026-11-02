import os
from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from uuid import uuid4

import pytest

from skytrace.config import Settings
from skytrace.history import HistoryStore, _WriteBatch, migrate
from skytrace.models import AircraftObservation, SourceAttribution

TEST_DATABASE_URL = os.environ.get("SKYTRACE_TEST_DATABASE_URL")
DRIVER_AVAILABLE = util.find_spec("psycopg") is not None
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not DRIVER_AVAILABLE, reason="install the history extra"),
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="set SKYTRACE_TEST_DATABASE_URL to an isolated PostGIS test database",
    ),
]


@pytest.fixture
async def history_database():
    psycopg = import_module("psycopg")
    sql = import_module("psycopg.sql")
    schema = f"skytrace_test_{uuid4().hex}"
    settings = Settings(
        history_enabled=True,
        history_database_url=TEST_DATABASE_URL,
        history_schema=schema,
        history_retention_days=1,
    )
    await migrate(settings)
    try:
        yield settings
    finally:
        connection = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, autocommit=True)
        async with connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def plane(
    icao24: str,
    observed_at: datetime,
    latitude: float,
    longitude: float,
    *,
    groundspeed: float,
) -> AircraftObservation:
    return AircraftObservation(
        icao24=icao24,
        source="synthetic",
        source_type="integration_test",
        provider_timestamp=observed_at,
        received_at=observed_at,
        position_observed_at=observed_at,
        latitude_deg=latitude,
        longitude_deg=longitude,
        geometric_altitude_m=1600.25,
        geometric_altitude_reference="wgs84_ellipsoid",
        groundspeed_mps=groundspeed,
    )


async def test_postgis_round_trip_dedup_order_retention_spatial_and_indexes(
    history_database: Settings,
) -> None:
    psycopg = import_module("psycopg")
    store = HistoryStore(history_database)
    attribution = SourceAttribution(
        name="synthetic",
        attribution="Integration-test synthetic data",
        url="https://example.test/source",
        license="MIT",
        license_url="https://example.test/license",
    )
    now = datetime.now(UTC).replace(microsecond=0)
    first = plane("000001", now - timedelta(minutes=2), 39.8561, -104.6737, groundspeed=101.5)
    duplicate = first.model_copy(update={"groundspeed_mps": 999.0})
    second = plane("000001", now - timedelta(minutes=1), 39.86, -104.67, groundspeed=102.5)
    distant = plane("000002", now - timedelta(seconds=30), 40.76, -111.89, groundspeed=103.5)
    old = plane("000003", datetime(2000, 1, 1, tzinfo=UTC), 39.85, -104.67, groundspeed=90.0)

    inserted = await store._insert(
        _WriteBatch((first, duplicate, second, distant, old), attribution)
    )
    assert inserted == 4

    aircraft = await store.aircraft(
        "synthetic", "000001", now - timedelta(hours=1), now + timedelta(minutes=1), 10
    )
    assert [item.position_observed_at for item in aircraft.observations] == [
        first.position_observed_at,
        second.position_observed_at,
    ]
    assert aircraft.observations[0].groundspeed_mps == 101.5
    assert aircraft.observations[0].geometric_altitude_m == 1600.25
    assert aircraft.observations[0].geometric_altitude_reference == "wgs84_ellipsoid"
    assert aircraft.attribution == attribution

    limited = await store.aircraft("synthetic", "000001", now - timedelta(hours=1), now, 1)
    assert limited.truncated is True
    assert limited.observations == (first,)
    half_open = await store.aircraft(
        "synthetic", "000001", first.position_observed_at, second.position_observed_at, 10
    )
    assert half_open.observations == (first,)
    assert half_open.truncated is False

    nearby = await store.positions(
        "synthetic",
        now - timedelta(hours=1),
        now + timedelta(minutes=1),
        39.8561,
        -104.6737,
        2_000,
        10,
    )
    assert [item.icao24 for item in nearby.observations] == ["000001", "000001"]

    await store._maybe_retain()
    old_rows = await store.aircraft(
        "synthetic",
        "000003",
        datetime(1999, 1, 1, tzinfo=UTC),
        datetime(2001, 1, 1, tzinfo=UTC),
        10,
    )
    assert old_rows.observations == ()

    connection = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL)
    async with connection:
        result = await connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s",
            (history_database.history_schema,),
        )
        definitions = "\n".join(row[0].lower() for row in await result.fetchall())
    assert "source, icao24, position_observed_at" in definitions
    assert "position_observed_at" in definitions
    assert "using gist (geom)" in definitions
