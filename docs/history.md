# Local history and operations

History is optional. The live cache, provider polling, WebSockets and globe run
without PostgreSQL or its Python driver. Recording starts only after explicitly
enabling it. It records future successful polls; it cannot reconstruct telemetry
that SkybyDay did not receive.

## Database setup

Use a local PostgreSQL server with PostGIS available. Verification used
PostgreSQL 18.6 and PostGIS 3.6.4 on Arch Linux. Provide a database and an account
allowed to create the `postgis` extension and SkybyDay schema for the explicit
migration. Normal operation needs connections, SELECT, INSERT and DELETE on
the history table; it performs no DDL.

For an existing server, set this in the ignored root `.env`, substituting your
local account and percent-encoding reserved characters in credentials:

```dotenv
SKYTRACE_HISTORY_DATABASE_URL=postgresql://skytrace:YOUR_PASSWORD@127.0.0.1:5432/skytrace
SKYTRACE_HISTORY_ENABLED=false
SKYTRACE_HISTORY_SCHEMA=skytrace_history
SKYTRACE_HISTORY_RETENTION_DAYS=7
```

A Unix socket URL is also supported, for example
`postgresql://skytrace@/skytrace?host=/run/postgresql` when your server's socket
and authentication policy match. Credentials never belong in Git or public URLs.

If Docker with Compose is already available, the repository includes an optional
database-only recipe. It binds port 5432 to loopback and stores data in the
`skytrace_history` named volume. Set the password in the shell, not the root
`.env` (which rejects unknown backend keys):

```bash
read -r -s -p 'Local database password: ' SKYTRACE_DB_PASSWORD
export SKYTRACE_DB_PASSWORD
docker compose config --quiet
docker compose up -d db
docker compose ps
```

Use the same password in `SKYTRACE_HISTORY_DATABASE_URL`. The recipe follows
the official [PostGIS image](https://github.com/postgis/docker-postgis)
`18-3.6` tag and PostgreSQL 18 volume root `/var/lib/postgresql`. This image tag
receives compatible updates; review and back up before pulling a new image.
The Compose container itself has not been run in this environment; real database
verification used a separate native PostgreSQL/PostGIS instance.

From the repository root, install and migrate:

```bash
uv sync --project backend --extra history --locked
uv run --project backend --extra history --locked skytrace-history migrate
```

The command is idempotent for schema version 1. It creates PostGIS, the table
and indexes if absent, and exits 2 on configuration/connection/migration failure.
It does not repair incompatible pre-existing tables or upgrade a future schema.
The SQL migration is included in the installed wheel and source distribution.

Set `SKYTRACE_HISTORY_ENABLED=true` and start one backend instance:

```bash
uv run --project backend --extra history --locked uvicorn skytrace.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --ws websockets-sansio --ws-max-size 1024 --timeout-graceful-shutdown 5
```

Keep `--extra history` on uv commands while recording. A uv sync/run without it
can remove the optional driver. Start the frontend as described in the README.
`GET /api/history/status` shows `starting` until the first operation, then
`available` or `degraded`. `/api/health` reports live ingestion separately.
If configuration is valid but the database or driver is unavailable, the live
service still runs; history requests return 503 and the writer retries finitely.

## Recording semantics and limits

- Store positioned observations from each successful provider batch once.
  Metadata-only records and browser-generated positions are not written.
- Preserve the complete canonical JSON, source, attribution, provider time,
  receipt time and position time. Deduplicate by source, ICAO24 and position
  timestamp. The first stored observation wins if a later poll repeats that key.
- The point uses longitude then latitude in WGS84 geography. Height remains in
  the canonical JSON with its datum; the 2D point does not assert ground height.
- One writer consumes at most eight queued batches plus one in flight. Each
  batch is bounded to 10,000 records. Full queues drop incoming batches;
  failed writes retry after 5, 10 and 20 seconds, then drop that batch.
- Database operations have five-second deadlines. Shutdown allows three
  seconds to drain; remaining work is cancelled and counted. A process crash
  loses queued work and counters. A connection failure around commit can make
  the outcome uncertain; deduplication makes a later retry safe.
- Status exposes accepted, written, duplicate, retry, failure and dropped
  counts. They are per-process operational indicators, not an audit ledger.
  Query or retention failure also marks the most recent storage operation
  degraded. A successful later operation clears the current error.

This is best-effort local recording. It is not a lossless aviation archive or
a flight-identity service. Callsign alone does not identify a flight. At the
default 30-second poll, one aircraft can contribute up to about 2,880 distinct
positions per day; choose area and retention to suit disk capacity. No large
cohort throughput benchmark has been performed.

Retention deletes at most 10,000 rows older than the configured position-age
cutoff after each committed batch. An idle database can retain expired rows
until the next write. SQL deletion permits PostgreSQL to reuse space; it does
not immediately shrink the data directory. Normal autovacuum remains necessary.
If retention fails, monitor `state`/`last_error` and disk usage; history has no
hard disk-size cap. Reducing retention intentionally removes older recordings.

## Query and replay

The [API contract](api.md#historical-observations) documents source-qualified
aircraft and radius queries. Windows are UTC `[start, end)`, at most 24 hours;
responses contain at most 5,000 original observations in time order and an
explicit `truncated` flag. Narrow the window if truncated. No bulk downloader,
automatic pagination or upstream historical fetch occurs.

The viewer's **History / replay** form uses UTC despite the browser's localized
date/time control. Select a source and ICAO24, then a range containing recorded
data. Playback begins paused at the first returned observation and ends at the
last. It uses an independent clock, interpolation only across acceptable short
segments, and holds across missing data. A gap of at least 60 seconds marks the
preceding observation stale. The inspector separates that observation from the
display position. The query can include multiple flights by the same aircraft;
SkybyDay does not label them as one flight.

Live snapshots continue in the background. **Return to live** restores the
live cache and its filters. Switching sources for a history query never changes
the live provider or makes a third-party request. Empty responses and failures
have distinct messages. Source/license attribution accompanies historical data.

## Recovery and backups

For a database outage, restore the configured database, then inspect
`/api/history/status`. Later queued/new batches retry automatically. No backend
restart is required for a transient database outage, and missing telemetry is
not backfilled. A wrong URL, missing driver or missing schema needs correction
and, where configuration changes, a backend restart. Keep the provider synthetic
while testing restart/error paths so these tests cannot bypass live quotas.

For the optional Compose service, `docker compose stop db` stops the database
without deleting its volume; `docker compose start db` resumes it. Ordinary
`docker compose down` preserves the named volume. Do not add `--volumes` when
you intend to keep recordings.

Use PostgreSQL's [backup/restore tools](https://www.postgresql.org/docs/current/backup-dump.html)
with appropriate local authentication. For the Compose recipe, an example is:

```bash
mkdir -p data/backups
docker compose exec -T db pg_dump -U skytrace -d skytrace -Fc > data/backups/skytrace.dump
```

Choose a new backup filename on each run. Test restores into a separate empty
PostGIS database before relying on them. Backups include provider data and its
licensing obligations. Backup/restore and Compose commands are operational
instructions, not claims that a recovery drill was executed in this release.

## Integration test

Use a separate disposable database with PostGIS available, never production.
Each test creates a random `skytrace_test_…` schema and drops only that schema
on completion; the extension can remain installed in the test database.

```bash
SKYTRACE_TEST_DATABASE_URL='postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/skytrace_test' \
  uv run --project backend --extra history --locked pytest backend/tests
```

Without the URL or optional driver, the one database integration test is
explicitly skipped; all offline tests still run. It checks round-trip SI values,
deduplication, chronological/half-open queries, truncation, metre-based spatial
selection, retention and indexes. Runtime failure/recovery and rendered replay
are separate evidence in [release verification](phase-6-verification.md).

## Hosting boundary

This release is local. A VPS installation would need an explicitly authorized
deployment with static hosting for `frontend/dist`, `/api` HTTP/WebSocket
proxying, TLS, access policy and backups. Keep one ingestion worker. Do not
publish the Vite development/preview server or the database port as a public
service. No VPS, hosted database or cloud account was created for this release.
