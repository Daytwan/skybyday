# Local API

Base URL: `http://127.0.0.1:8000`. Vite proxies `/api` HTTP requests and
WebSocket upgrades. The service runs one lifespan-owned poller; every endpoint
reads its shared cache. Default telemetry is live ADSB.lol, with no synthetic
fallback. The version-1 REST/WebSocket telemetry envelope and polling cadence
remain unchanged by the map/UI release. Weather and route references use separate
bounded shared caches described below.

## Health and discovery

`GET /api/health` → HTTP 200, JSON, `Cache-Control: no-store`:

```json
{
  "status": "ok",
  "service": "skytrace",
  "version": "0.1.0",
  "milestone": "backend-api",
  "telemetry": "adsblol",
  "provider_status": "healthy",
  "poller_running": true,
  "cached_aircraft_count": 2,
  "area": {
    "latitude_deg": 39.8561,
    "longitude_deg": -104.6737,
    "radius_nm": 60
  },
  "poll_interval_seconds": 30.0
}
```

`status` means the application can answer. It does not promise fresh positions.
`telemetry` is `synthetic` or `adsblol`; `provider_status` is `idle`, `healthy`,
`degraded` or `suspended`. `poller_running` is independent: an unexpected
programming failure stops the poller but leaves cached data available. Inspect
source times and errors in the aircraft snapshot for freshness.

`GET /api/openapi.json` exposes the generated HTTP schema, including canonical
model fields. `GET /api/docs` serves Swagger UI (its assets use a CDN).
WebSockets are documented here because OpenAPI does not describe that protocol.
Unknown HTTP routes return 404. With the lifespan disabled, data and health
routes return 503; no request starts ingestion implicitly.

## Current aircraft

`GET /api/aircraft` → HTTP 200 with a full `SnapshotMessage` and
`Cache-Control: no-store`. This is the same envelope sent over WebSockets.
An illustrative initial response before the first fetch is:

```json
{
  "type": "snapshot",
  "schema_version": 1,
  "stream_id": "30da5350-b337-4c17-ade6-1eb59cd77f83",
  "sequence": 1,
  "poller_status": "running",
  "tracking": {
    "area": { "latitude_deg": 39.8561, "longitude_deg": -104.6737, "radius_nm": 60 },
    "poll_interval_seconds": 30.0,
    "stale_after_seconds": 90.0,
    "expire_after_seconds": 300.0
  },
  "data": {
    "emitted_at": "2026-10-19T06:00:00Z",
    "source": "adsblol",
    "status": "idle",
    "last_error": null,
    "consecutive_failures": 0,
    "last_success_at": null,
    "retry_in_seconds": 0.0,
    "batch": null,
    "aircraft": [],
    "stale_aircraft_count": 0
  }
}
```

After a successful fetch, `data.batch` contains source attribution/license,
provider and receipt times, raw/rejected counts, rejection reasons and selected
HTTP metadata. `data.aircraft` contains the canonical
[observations](data-model.md), with nullable unknown fields and SI units.
A valid empty response has `status=healthy` and batch metadata; an outage has
`status=degraded` or `suspended`, with an error and any unexpired cached aircraft.
`data.batch` then continues to describe the last successful response.

`poller_status` is `running`, `failed` or `stopped`. `stream_id` changes on every
lifespan/restart. `sequence` increases for state changes within that stream;
it is not a permanent event ID or a count of provider requests. Reading the
same state updates `emitted_at` and `retry_in_seconds` without increasing the
sequence. Cache expiry/stale transitions can increase it without a network fetch.
A retry delay is relative to `emitted_at`, not a promise to poll at an exact time.

Phase 4 adds `tracking` to this version-1 envelope. It exposes the configured
area, poll interval, stale threshold and expiry threshold in seconds. The
browser requires these fields to age records during disconnection, rather than
assuming default server settings. They are fixed for a lifespan. Deploy matching
backend/frontend versions; older clients can ignore this additive field.

`GET /api/aircraft/{icao24}` → HTTP 200 with:

- `schema_version`, `stream_id`, `sequence`, `emitted_at`;
- `source` attribution, `provider_status`, and a `stale` boolean;
- `aircraft`: one canonical observation.

The identifier must be six lowercase hexadecimal digits (leading zeros retained).
Invalid identifiers return 422; absent/expired aircraft return 404. Successful
and missing-aircraft responses have `Cache-Control: no-store`. The route never
fetches on demand. Missing positions are stale; the default position-age stale
threshold is 90 seconds. The cache expires observations at 300 seconds.

## Route references

`GET /api/aircraft/{icao24}/route` returns a `RouteResponse` with
`Cache-Control: no-store`. The aircraft must be in the current telemetry cache:
invalid identifiers return 422, absent/expired aircraft return 404. Its current
callsign supplies the lookup; callers cannot specify an arbitrary external URL
or callsign. Synthetic observations return an unknown reference without a lookup.

The response contains `callsign`, `status` (`reference`, `unknown`, `unavailable`
or `waiting`), `airports` (ordered objects with `icao`, nullable `iata`, and `name`),
`source`, `source_url`, nullable `fetched_at`, `retry_in_seconds`, and
`current_flight_confirmed: false`.

This is an ADSB.lol mirror of VRS callsign standing data, **not a live flight plan**.
Two-airport references can be displayed as departure/destination with that
qualification. More than two airports cannot identify the current leg. Unmatched,
private or ambiguous aircraft retain Unknown. Replay does not borrow this data.

One process-wide lock and a ten-second minimum gap bound upstream lookups.
The LRU cache retains at most 256 callsigns: six hours for a valid result, one hour
for 404, five minutes for failures. An upstream failure applies a five-minute
global delay; `Retry-After` can extend it. Replies are capped at 64 KiB with a
ten-second overall deadline. This endpoint never triggers an aircraft poll.

## Denver weather

`GET /api/weather` returns a `WeatherResponse` with `Cache-Control: no-store`:
`source: "NWS"`, `source_url`, `status` (`available`, `unavailable`, `degraded`),
nullable `observation`, nullable `fetched_at`, `stale`, `retry_in_seconds`, and
`message`. It always describes **KDEN**, independently of camera or tracking area.

An observation includes `station`, UTC `observed_at`, `description`, nullable
`temperature_c`, `dewpoint_c`, `wind_direction_deg`, `wind_speed_mps`,
`wind_gust_mps`, `visibility_m`, `cloud_cover` and `cloud_base_m`. Unknown units,
invalid values and rejected quality flags stay null. Clear-sky reporting limits
are not cloud bases. The first reported cloud layer is retained; it is not
necessarily a ceiling. Observations over 90 minutes old are marked stale.

A shared lock/cache permits one upstream request per ten minutes, regardless of
viewer count. Errors retain the previous observation and back off from ten minutes
to one hour plus jitter, respecting longer `Retry-After` delays. Requests have a
ten-second overall deadline and 256 KiB body cap. No synthetic weather or global
weather coverage is implied. NWS access needs no account; its required identifying
User-Agent is sent. Weather errors do not affect aircraft ingestion.

## WebSocket snapshots

Connect to `ws://127.0.0.1:8000/api/ws`, or `/api/ws` through Vite on port 5173
or preview on 4173. Browser clients must send an exact allowed Origin; defaults
cover `http://127.0.0.1` and `http://localhost` on ports 8000, 5173 and 4173.
Configure `SKYTRACE_WS_ALLOWED_ORIGINS` as a JSON array of HTTP(S) origins.
Missing, `null`, duplicate and unlisted origins are rejected before acceptance.
Non-browser clients must also supply an allowed Origin; this is not authentication.

The server sends an initial full snapshot, then full snapshots on observations,
provider/poller status, staleness or expiry changes. The initial state can be
idle/empty while the first request is in flight. No client subscription command
or application heartbeat is needed. Any incoming text/binary application message
closes the connection; protocol control frames are handled by the transport.

Each client holds one pending snapshot plus a possible in-flight send. Newer
updates replace pending older ones. Sequence gaps are expected: replace local
membership from each full snapshot. On reconnect, accept the fresh full snapshot
and reset sequence tracking if `stream_id` changes. Reconnect with bounded
backoff; reconnecting never triggers a telemetry request.

Default bounds are 32 accepted clients and a 5-second send/close deadline.
The documented Uvicorn command sets an incoming-message limit of 1,024 bytes.
A client that cannot drain data is disconnected once a send stalls beyond the
deadline; this bounds application buffering, not the operating system's buffers.

| Condition | Observed transport behavior |
| --- | --- |
| Disallowed/missing Origin | HTTP 403 handshake denial |
| Connection capacity reached | HTTP 403 handshake denial (pre-accept close with code 1013) |
| Unsupported client application message | Close 1008 |
| Incoming message over the configured transport limit | Close 1009 |
| Send timeout | Close 1013 where transport permits |
| Runtime stops | Close 1001; Uvicorn process shutdown normally closes first with 1012 |

Tests using Starlette's in-process client can expose the pre-accept close code
instead of the real HTTP handshake status. Slow network behavior is separately
covered with a deliberately blocked ASGI send and real process checks.

## Browser and operating model

The viewer requests `/api/aircraft` on load with a five-second timeout, then
subscribes to `/api/ws`. It validates the fields it consumes, rejects malformed
frames, and inserts provider text through DOM text content. It accepts only
increasing sequences within a stream, replacing membership on each accepted
snapshot. A new stream resets sequence tracking.

Connection loss or an invalid frame triggers jittered exponential reconnects:
1–1.2 seconds initially, doubling to a 30-second total cap. Backoff resets only
after a valid snapshot. A connection without its first snapshot closes after
10 seconds. There is one active connection per page and no application heartbeat.
Reconnecting reads the shared cache; it does not request source telemetry.

The browser ages observations from server `emitted_at` plus locally elapsed
monotonic time, avoiding dependence on its wall-clock offset. It marks stale and
removes expired records while disconnected. This ignores network transit time;
it is an approximate display-age indicator, not a precision synchronization
protocol. Phase 5 keeps the latest source observation separate from a derived
display position. Interpolation is enabled by default unless the browser
prefers reduced motion or the user disables it. The display timeline is delayed
by the poll interval plus five seconds, capped at 60 seconds; at the default
30-second poll it is 35 seconds behind. The browser keeps at most four samples,
uses WGS84 geodesics only across gaps of 60 seconds or less, applies a 1,500 m/s
horizontal jump guard, linearly interpolates compatible geometric heights, and
holds at the ends of the sample interval. It never extrapolates indefinitely,
and stale or expired records disable/remove the display state as before.

The inspector reports the latest observation time and the display time
separately, with a reason such as waiting for a second sample, interpolation,
holding the latest observation or a rejected segment. Display controls can hide
geometric height, change label visibility and show a fixed 4 km cue based on the
latest reported ground track. The cue is not an inferred heading or predicted
flight path. Search matches callsign, ICAO24 or aircraft type literally and
case-insensitively; the fresh-only option filters stale records. These controls
do not modify API data or request additional provider snapshots.

No CORS origins are enabled. Run on loopback with **one worker and one service
instance**. Multiple processes would each poll the source. Retry deadlines and
cache state reset on restart, so use synthetic mode for auto-reload development.
No provider API key, authentication or fallback provider is configured. Optional
history is described below.

## Historical observations

History endpoints query the local PostGIS database. They never poll ADSB.lol or
fetch an upstream archive. Install the history extra, migrate explicitly and
enable recording as described in [history operations](history.md).

`GET /api/history/status` returns HTTP 200 with:

- `enabled` and `state`: `disabled`, `starting`, `available`, `degraded` or `stopped`;
- `queue_capacity_batches` (8), `queued_batches`;
- `accepted_batches`, `accepted_observations`, `written_observations`,
  `duplicate_observations`, `retry_count`, `failure_count`;
- `dropped_queue_batches`, `dropped_queue_observations`,
  `dropped_write_observations`, `dropped_shutdown_observations`;
- nullable `last_write_at` and sanitized `last_error` (no DSN or credentials).

Counters reset on backend restart. Live health and storage health are independent.

`GET /api/history/aircraft/{icao24}` requires `source` (`synthetic` or `adsblol`),
`start` and `end`. The ID is six lowercase hexadecimal digits. Times must have
UTC offsets (`Z` or `+00:00`); `[start, end)` must be positive and at most 24 hours.
`limit` defaults to 1,000 and accepts 1…5,000. Example:

```text
/api/history/aircraft/000001?source=synthetic&start=2026-10-19T23:00:00Z&end=2026-10-20T00:00:00Z&limit=1000
```

The version-1 response contains `schema_version`, `source`, `icao24`, `start`,
`end`, `truncated`, `observations` and `attribution`. Observations use the canonical
aircraft model and are ordered by position timestamp ascending. `truncated=true`
means later rows exist: narrow the query window for a complete result. Valid
empty results are HTTP 200 with `observations=[]`, `attribution=null` and
`truncated=false`. Nonempty results carry recorded source attribution.

`GET /api/history/positions` requires the same `source`, `start`, `end` and
optional `limit`, plus `latitude_deg` (−90…90), `longitude_deg` (−180…180) and
`radius_m` (>0…500,000). Coordinates must be finite. The PostGIS geography radius
query uses metres, ordered by position time then ICAO24. Its response has the
same fields except no top-level `icao24`; each observation retains its ID.
Altitude does not affect this 2D surface-distance query.

Invalid parameters return 422. Disabled, unavailable or unmigrated storage
returns 503 with `{"detail":"Trajectory history is unavailable"}`. Successful
responses and storage status set `Cache-Control: no-store`. Queries have an
overall five-second deadline, including waiting for one of two query slots.
There is no public migration, deletion or configuration route.

The browser requests at most 5,000 observations with a five-second timeout,
validates source/ID, chronology, window, coordinates and attribution links, then
uses an independent replay clock. Long gaps and the final sample hold position;
there is no extrapolation beyond the final observation. Filtering is disabled
during replay and restored on returning to live. The historical marker is one
aircraft at a chosen time, not a claim of a complete identified flight.

## Fleet routes and sampled coverage (24 October 2026)

`GET /api/routes` is a zero-upstream-request projection of all currently cached
aircraft through the bundled VRS standing index. It returns `schema_version`,
`stream_id`, `sequence`, `emitted_at`, `source_ready`, `tracked_aircraft`,
`two_endpoint_references`, `multi_leg_references`, `unknown`, `unavailable`,
`confirmed_current_flights: 0`, and `references: [{icao24, route}]`. The route
object uses the same model as the per-aircraft endpoint, including source
commit/retrieval time and airport coordinates when present. No current flight
identity is confirmed, and synthetic observations are not enriched as real flights.

Snapshot schema version 1 gains additive fields. `tracking` includes
`coverage_preset`, `coverage_kind`, `coverage_cycle_seconds`, `coverage_area_count`;
legacy `area` remains the fixed primary area. `data.coverage` contains each
scope's identity, circle, status, last successful time/error, freshness age and
stale flag. `active_scope_id` identifies the next scope due. Collection metadata
includes `collected_aircraft_count` (retained scope memberships, possibly duplicate
identities), `published_aircraft_count`, `truncated_aircraft_count` (omitted
observations; overlap can count more than once) and `aircraft_capacity`.

The six-hub preset has one shared request slot, per-scope replacement, freshest
identity deduplication and bounded retention; camera movement never subscribes
new regions. Scalar age changes do not trigger full WebSocket broadcasts.
See [implementation and evidence](world-experience-verification-2026-10-24.md).
