# Milestone roadmap

Updated 24 October 2026: the multi-part world experience package adds full local
VRS fleet references, six sampled hubs, NASA Earth imagery, finer terrain meshes,
and reference/follow controls. See [world experience verification](world-experience-verification-2026-10-24.md).
Confirmed routes for every flight, continuous global traffic and full geographic
fidelity remain open source/acceptance gates.

Work sequentially. Each phase ends with relevant formatting/lint, automated
checks, startup/API smoke checks, affected browser behavior, and error recovery.
Keep synthetic tests independent of public APIs; record live checks separately.

| Phase | Deliverable | Acceptance gate |
| --- | --- | --- |
| 0 — Research | Architecture, provider/access notes, SI model design, ADRs | Official sources reviewed; uncertainties and live MVP scope explicit |
| 1 — Foundation | Git, uv/FastAPI skeleton, TypeScript/Vite status screen, environment config and tests | Locked installs; tests/lint/build; local health and browser success/failure/recovery verified |
| 2 — Ingestion | Provider interface, ADSB.lol adapter, synthetic adapter, typed aircraft observations, bounded cache and polling policy | Fixtures cover units, missing data, invalid coordinates, ground altitude, non-ICAO IDs, source time encoding, 429/Retry-After, 5xx, backoff and recovery; one deliberate live fetch normalizes real data |
| 3 — Backend API | Lifespan poller, cached list/detail routes and WebSocket snapshots | Multiple clients share one poller; zero-aircraft success differs from outage; slow/disconnected clients bounded; cancellation and shutdown tested |
| 4 — Basic 3D radar | Cesium globe centered on DEN, real aircraft markers, selection metadata | Browser renders globe and correctly located/altitude-qualified markers; selection/update works; no mandatory cloud token; asset paths/build tested |
| 5 — Movement | Timestamp-aware interpolation, labels/track indicators, filtering and display controls; retain Phase 4 stale removal | Two-sample interpolation, gap limits and outage behavior verified with a synthetic clock; no invented pitch/roll; actual browser interaction checked |
| 6 — Persistence and replay | Optional PostGIS schema, bounded recorder, retention, spatial/trajectory queries and single-aircraft replay | Real database round-trip/dedup/index/retention tests; storage failure isolation and actual browser playback verified |
| QoL 1 — Map quality (priority release) | US aerial imagery, open terrain, detailed FAA DEN overlay, aircraft silhouettes, camera shortcuts and saved map preferences | Actual airport/terrain rendering; synthetic aircraft selection; offline fallback; provenance, units, errors and mobile usability |
| 7 — Optional extensions | OpenSky, airport proximity, weather, analytics/ML, models or local receiver | Select one bounded feature at a time; revisit its licensing, accuracy and performance requirements |

## Current boundary

The local release implements Phases 0–6, including single-aircraft historical
replay. Phase 5/6 local acceptance passed as recorded in the
[release verification](phase-6-verification.md).
Phase 2 verified the millisecond timestamp encoding
with one live DEN response and normalized 55 of its 56 records; one non-ICAO
address was explicitly skipped. Phase 3 connected the service to the FastAPI
lifespan and added cached list/detail endpoints and bounded WebSocket snapshots.
Shared polling, outages, client limits, slow/disconnected clients, a real
WebSocket proxy and shutdown passed with synthetic data. See the
[Phase 2 evidence](phase-2-verification.md) and
[Phase 3 evidence](phase-3-verification.md). Phase 4 added a token-free Cesium
globe, local assets, validated REST/WebSocket consumption, selectable markers,
metadata, reconnects and client-side stale/expiry handling. Actual browser checks
covered synthetic updates, selection, camera controls, disconnection/expiry,
recovery and the production build. One live DEN request returned 130 aircraft;
100 had geometric height and 30 were explicitly projected onto the reference
ellipsoid. See the [Phase 4 evidence](phase-4-verification.md).

Phase 5 adds bounded, timestamp-aware interpolation between two observations,
configurable labels and ground-track indicators, basic filters and visualization
controls. The implementation preserves unknown altitude/orientation and
distinguishes rendered positions from source observations. Deterministic motion
tests and actual browser interactions cover the movement layer. The final
production-build, mobile and outage checks are recorded with the
[release verification](phase-6-verification.md). No additional live provider
request was made for Phase 5 or 6.

Phase 6 adds optional PostGIS recording, explicit migration, deduplication,
retention, bounded history APIs and browser replay. A real local database passed
round-trip, chronology, query bounds, spatial selection and retention checks.
Stopping the database did not stop live polling; history queries failed clearly
and recording recovered after restart. Playback, seeking, final-sample holds,
return-to-live, empty history and unavailable storage were exercised in the
built browser. The database is not required for the live viewer. See
[history operations](history.md) for configuration and evidence limits.

## Live DEN MVP

The live MVP spans Phases 2–5: one local service obtains a DEN-area snapshot,
normalizes it with source/age information, shares it across clients, and renders
selectable aircraft on a globe with bounded interpolation and stale handling.
If no real positions are returned, report that accurately and use explicitly
labeled synthetic data for repeatable UI tests; an empty live response is not
proof of the live visualization acceptance criterion.

Persistent history, flight routes/destinations, weather, ML, authentication,
public hosting and high-availability deployment are outside that MVP.

## Priority sequence after map quality

The user's priority is map quality and everyday usability before widening the
traffic footprint. The QoL 1 implementation is documented in [maps](maps.md).
Subsequent increments should stay separately reviewable:

1. **QoL 2:** airport/aircraft search and follow controls, saved display/units
   preferences, better selected-aircraft trails and camera state. Extend airport
   detail through bounded, attributed extracts. Evaluate legally usable global
   high-resolution imagery separately; the current aerial detail is US-focused.
2. **Coverage 1:** configurable regional presets and explicit coverage/freshness
   indicators. Keep one shared provider schedule and cache; moving a camera must
   not start a provider poll per browser.
3. **Coverage 2:** deduplicated regional subscription areas with a global request
   budget, fair scheduling and bounded memory. Benchmark progressively larger
   recorded/synthetic cohorts before changing snapshot transport or Cesium entities.
   Revisit deltas, level-of-detail and clustering only against measurements.
4. **Coverage 3:** an authorized feed suitable for worldwide ingestion, with
   documented quotas, attribution, retention and operating cost. Add source
   deduplication, regional coverage metadata and missing-data visibility. Do not
   stitch unlimited regional requests into an undocumented bulk-download API.

“Global” means all supported observed traffic within documented coverage, not
every aircraft in existence: receiver coverage, equipage, provider restrictions,
delays and outages make literal complete coverage impossible to guarantee.
No global ingestion, new credentials or external infrastructure is implemented
by the map release.

An OpenSky adapter needs a fresh
review of OAuth, quotas and data terms before implementation. Airport proximity
and analytics can consume the existing source-qualified spatial/time API;
evaluate data quality and trajectory gaps before deriving events or training
models. Historical query responses provide a foundation for numerical analysis
without requiring NumPy in the live service. No ML model, inferred current flight route, global weather overlay or fallback aircraft provider is implemented. Local KDEN weather observations and published callsign route references are now available.

Public hosting and a personal VPS remain separate, explicitly authorized work.
The optional Compose recipe and backup instructions are provided, but container
startup and a backup/restore drill were not part of the tested native database
path. The local release does not claim those deployment checks.

## Local experience release — October 2026

Implemented: live-by-default startup, unified Traffic/Map/Weather/Replay workspace, aviation units, saved display preferences, responsive layout, NAIP DEN detail, progressive global city labels, partial Denver building volumes, Front Range peak labels, shared NWS station observations, and bounded route-reference lookups. See [verification](local-release-verification.md) and [source constraints](maps.md).

Next quality work: complete current-environment PostGIS replay acceptance, expand high-resolution imagery and measured building/landmark coverage through licensed or self-hosted datasets, then add regional traffic presets under the existing shared request budget. Global weather/radar and photorealistic landmark meshes are separate source/quality milestones. Keep deployment local until explicitly authorized; no VPS login is permitted by the current user instruction.
