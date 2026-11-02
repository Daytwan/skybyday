# Architecture decision records

Accepted for the local foundation and ingestion on 2026-10-18, extended for the
backend API, renderer and movement display on 2026-10-19. Revisit only when a milestone
provides a concrete reason. Exact installed versions live in the lockfiles.

## ADR 001 — One Python process with async I/O

Use Python 3.12+, FastAPI and Pydantic v2. Pin the local baseline to Python 3.12.
Use FastAPI's [lifespan mechanism](https://fastapi.tiangolo.com/advanced/events/)
for the poller and [one reusable HTTPX AsyncClient](https://www.python-httpx.org/async/).
Native asyncio is sufficient for sequential polling and cancellation; no Celery,
APScheduler, Redis or message broker is justified. One worker is an intentional
constraint until shared ingestion is externalized.

Use `pydantic-settings` for validated environment and `.env` loading rather than
maintaining a custom parser. Phase 2 promoted the existing HTTPX dependency
from development (ASGI tests) to runtime for provider access; no new dependency
was needed for ingestion or scheduling.
Uvicorn runs the service without its optional standard dependency bundle.

## ADR 002 — Framework-free browser and deferred Cesium

Use TypeScript and [Vite](https://vite.dev/guide/) with plain DOM APIs. The small
status screen and future Cesium canvas do not justify React or another UI
framework. Use relative API URLs with a local
[Vite proxy](https://vite.dev/config/server-options/#server-proxy), avoiding
cross-origin configuration for local development.

[Node 24 is LTS](https://nodejs.org/en/about/previous-releases) at this review;
pin its major for development. Use Node's native test runner for the small
network contract suite. Biome provides TS/CSS linting and formatting in one
development tool; Ruff covers Python. No browser runtime libraries are needed
for Phase 1.

CesiumJS is the Phase 4 renderer, not a Phase 1 dependency. Its
[Apache-2.0 code license](https://github.com/CesiumGS/cesium/blob/main/LICENSE.md)
is separate from imagery/terrain licenses. A
[Viewer with no base layer](https://cesium.com/learn/cesiumjs/ref-doc/Viewer.html)
and [ellipsoid terrain](https://cesium.com/learn/cesiumjs/ref-doc/EllipsoidTerrainProvider.html)
can avoid an ion account. Add local imagery or a separately approved provider
later; never assume free public tile servers permit unlimited use. Bundle
Cesium's workers and static assets when integration begins.

## ADR 003 — Shared snapshots and explicit source truth

Use one bounded in-memory cache and full snapshot WebSocket messages initially.
This keeps reconnect/recovery simple and prevents provider calls per browser.
Observations and interpolation are distinct. Keep canonical SI values, degrees
for geographic/heading interchange, UTC timestamps, and source provenance.
Ground track and true/magnetic heading have separate fields.

Add PostgreSQL/PostGIS only when historical queries are needed. Do not add an
unused repository interface or empty storage module. NumPy also waits for an
actual numerical workload.

## ADR 004 — Reproducible local projects and separate licenses

Keep `backend/` and `frontend/` at the existing repository root rather than add
another `skytrace/` directory. Commit `uv.lock` and `package-lock.json`; use
[uv locked synchronization](https://docs.astral.sh/uv/guides/projects/) and
`npm ci` for repeatable installs. No Docker services are needed yet.

Choose MIT for original SkybyDay source code. Provider data retains its own
license, including ADSB.lol ODbL requirements; it is not relicensed by the
repository's MIT license. Downloaded telemetry and historical datasets are
excluded from the repository by default.

## ADR 005 — Verify ingestion before attaching the web service

Expose Phase 2 through a one-shot `skytrace-ingest` CLI with synthetic data by
default. A provider makes one bounded request; `IngestionService` owns the
monotonic deadline, backoff and cache, and can be tested with injected clocks.
Keep the FastAPI health contract stable until Phase 3 owns the polling lifecycle
and subscriber limits. This gives ingestion a concrete consumer and a deliberate
live verification command without prematurely adding continuous network access.

Use one `models.py` for the current canonical types and a small `providers/`
and `services/` package. Add modules only when they have implemented consumers.
Keep the previous whole observation if an incoming position is missing or older;
this avoids pretending old coordinates belong to newer metadata.

## ADR 006 — Bounded full snapshots and an explicit WebSocket transport

Phase 3 adds `websockets` as one runtime dependency because Uvicorn's minimal
installation has no WebSocket transport. Use its explicit
[`websockets-sansio` implementation](https://uvicorn.dev/settings/), avoiding the
legacy transport API. No broker, subscriber framework or scheduler is needed.

The lifespan owns one poller; readers consume its cache. One latest-only queue
per client, a client-count limit and send timeout bound pending delivery. Send
complete versioned snapshots on connection and state changes, with a unique
stream ID per lifespan and sequence per state change. This tolerates dropped
intermediate frames without keeping a replay log. Origin checks precede
acceptance; they are a browser boundary, not user authentication. REST retains
the same source/freshness data and never triggers a provider request.

`/api/health` reports application availability separately from provider status
and whether the poller is running. Update the small status-page parser alongside
that contract. Phase 4 replaces that screen with the aircraft viewer below.

## ADR 007 — Local Cesium assets and explicit display height

Phase 4 adds the pinned `cesium` package as the sole direct frontend runtime
dependency. Plain TypeScript/DOM remains sufficient. Follow the official
[Vite example](https://github.com/CesiumGS/cesium-vite-example) for
`CESIUM_BASE_URL` and the Workers/Assets/Widgets/ThirdParty directories. A small
built-in Node copy script handles assets before dev/build/preview; no copy plugin
or hosted CDN is needed. Keep Cesium's license and third-party notices with the
assets. Current hosting assumes the site root `/`.

Use [ellipsoid terrain and no default base layer](https://cesium.com/learn/cesiumjs/ref-doc/Viewer.html),
then load bundled Natural Earth II through
[`TileMapServiceImageryProvider`](https://cesium.com/learn/cesiumjs/ref-doc/TileMapServiceImageryProvider.html).
Natural Earth's [official terms](https://www.naturalearthdata.com/about/terms-of-use/)
place its raster/vector maps in the public domain (reviewed 2026-10-19). We retain
its credit. These coarse global tiles do not provide street/runway detail or
terrain elevation. No ion credentials, external tile requests, imagery service
subscription or new access policy is required for the local viewer.

Use point entities first and Cesium's
[`requestRenderMode`](https://cesium.com/learn/cesiumjs/ref-doc/Scene.html#requestRenderMode)
to redraw on data/camera changes. Feed
[`Cartesian3.fromDegrees`](https://cesium.com/learn/cesiumjs/ref-doc/Cartesian3.html#fromDegrees)
longitude, latitude and confirmed ellipsoid height. Unknown-height positions
are explicitly hollow surface projections; pressure altitude is metadata only.
No pitch/roll/heading is fabricated. Phase 5 extends the renderer with bounded
display interpolation and a separately identified ground-track cue; it does not
change the source observation contract.

Extend the snapshot with configured display retention/area policy so disconnected
clients expire observations consistently. Keep a validated snapshot and one entity
per positioned aircraft; cap the DOM list at 100 rows while retaining all markers.
This is sufficient for the initial bounded regional cache; benchmark real larger
cohorts before introducing clustering, primitives or another state library.

## ADR 008 — Delayed, bounded display motion

Phase 5 adds a browser-only movement layer in `aircraft/state.ts` and
`aircraft/motion.ts`; the FastAPI and WebSocket schemas remain unchanged. The
display timeline is delayed by the configured poll interval plus five seconds,
capped at 60 seconds. Keep at most four timestamped position samples per
aircraft, connect only adjacent samples no more than 60 seconds apart, and use
Cesium's [`EllipsoidGeodesic`](https://cesium.com/learn/cesiumjs/ref-doc/EllipsoidGeodesic.html)
for the WGS84 horizontal path. Interpolate geometric height linearly only when
both endpoints have the same height availability. Reject horizontal jumps over
the conservative 1,500 m/s display guard, hold at timeline boundaries and never
extrapolate indefinitely. Stale observations disable motion and expiry still
removes records locally.

This is a presentation estimate, not an additional telemetry measurement. The
latest canonical observation, its source timestamp and the derived display
position/time are retained separately in the inspector. A fixed 4 km cue uses
the latest reported ground track only; it is not a speed vector, predicted
destination or inferred body heading. A Cesium
[`CallbackProperty`](https://cesium.com/learn/cesiumjs/ref-doc/CallbackProperty.html)
backs the cue positions so the request-render-mode scene can update during
interpolation without rebuilding asynchronous static geometry. The cue is drawn
with Cesium's [`PolylineGraphics`](https://cesium.com/learn/cesiumjs/ref-doc/PolylineGraphics.html)
and is opt-in. No runtime dependency was added.

## ADR 009 — Optional PostGIS history with bounded recording

Phase 6 uses PostgreSQL/PostGIS only when history is enabled. Add the optional
`psycopg[binary]` extra for its [native async interface](https://www.psycopg.org/psycopg3/docs/advanced/async.html);
avoid an ORM, pool, migration framework or message broker for one writer and two
concurrent queries. An explicit packaged SQL migration is sufficient for the
single initial schema; future incompatible changes require numbered migrations.

Keep canonical observations and source/license metadata as JSONB, with separately
indexed identity/time columns and WGS84 geography. This preserves model fields
without prematurely flattening an analytics warehouse. Use longitude/latitude
order with [`ST_MakePoint`](https://postgis.net/docs/ST_MakePoint.html); radius
queries use [`ST_DWithin`](https://postgis.net/docs/ST_DWithin.html) geography in
metres and a GiST index. Deduplicate source/ICAO/position-time with the primary key,
first write wins. The model preserves altitude datum outside the 2D geography.

Offer each successful provider batch once to a finite queue; never await disk
from the poller. Finite write retries, deadlines, retention and loss counters
make outages bounded and visible while live display remains available. This is
best-effort recording; a durable queue would be a separate requirement if lossless
ingestion were necessary. Use real PostGIS tests for SQL behavior, synthetic
tests for backpressure/shutdown, and process/browser checks for isolation.

Queries return at most 5,000 samples for a UTC window of at most 24 hours.
Browser replay owns an independent clock and reuses the existing short-segment
interpolation policy. It starts paused, holds at gaps/end, keeps previous source
observations distinct from display coordinates, and restores the latest live
cache on exit. No server animation frames, historical provider downloads or
new frontend dependencies are needed. A small database-only Compose recipe is
optional; local native PostgreSQL is equally supported.

## ADR 010 — Open map detail with local fallbacks

For the next quality-of-life release, replace the exclusively coarse base map
with USGS's public cached aerial imagery, open Mapzen Terrarium elevation, and a
bounded FAA DEN airport snapshot. Keep Natural Earth II and the flat ellipsoid as
offline/failure fallbacks. No additional dependency or hosted account is needed.
Cesium already provides the imagery, heightmap, GeoJSON and billboard primitives.
See [source/access research and limitations](maps.md).

Separate browser map-layer requests from the shared aircraft poller. Limit
imagery to its advertised cached detail and terrain to level 15 with 129 × 129
samples. Do not build a prefetch service. Surface failures in the controls and
require explicit retries rather than generating an uncontrolled request loop.
Keep source terms and effective dates with the bundled airport data; it is
reference geometry and is not relicensed as MIT.

Terrain remains a visual approximation: no geoid correction, altitude-over-ground
or clearance calculations. Canonical observations remain unchanged. Original SVG
glyphs use exact known aircraft type codes, with an explicit generic fallback;
true heading and ground-track orientation remain distinguishable. Persist only
map preferences in versioned browser storage. Global traffic coverage is a later
ingestion/coverage milestone, not implied by a globe or world terrain layer.

## ADR 011 — Live defaults and bounded contextual detail

October 21: make ADSB.lol the backend and CLI default, superseding the initial
synthetic default in ADR 005. Keep explicit synthetic fixtures for reproducible
tests/offline development, never as provider-failure fallback. Preserve the
existing polling, snapshot, freshness and optional recording contracts.

Use one framework-free explorer with Traffic, Map, Weather and Replay tabs,
aviation display units and saved preferences. Keep the map central and the
aircraft inspector separate from navigation. Mobile places the explorer below
the globe; keyboard tabs, visible focus, Escape close and reduced-motion behavior
are part of the same interface. Original silhouettes scale down with camera
distance using Cesium's [billboard scale](https://cesium.com/learn/cesiumjs/ref-doc/BillboardGraphics.html#scaleByDistance)
to keep a regional view readable. No extra production library is needed.

Add finer NAIP exports only within DEN's bounded envelope; retain cached general
imagery beneath them. Bundle GeoNames geographic chunks and a bounded OSM Denver
extract rather than query public geocoding/Overpass per viewer. Render buildings
only when their source provides explicit height; do not invent photorealistic
geometry or promise universal global detail. Keep licenses/manifests separate.

Weather and route references use independent, serialized backend caches. NWS
station weather is KDEN-only and timestamped. VRS callsign standing data is
unconfirmed reference information, not live dispatch intent. Only a two-airport
match supplies departure/destination candidates; multi-leg matches preserve the
chain with the current leg unknown. Never attach a present-day route to replay.
Provider failures leave Unknown/old data with visible status. These services
share a separate two-connection client and cannot multiply telemetry polling.

Continue local verification before deployment. A ten-viewer regional benchmark
can inform a VPS estimate, but it cannot establish current server headroom,
WeBWorK peak demand or global-feed capacity. The user's no-login instruction
rules out further server inspection. No deployment or public service exposure
is part of this local release.
