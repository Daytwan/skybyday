# Aircraft data model

Implemented in `backend/src/skytrace/models.py` and emitted by the CLI. Phase 3
also exposes these observations in a versioned REST/WebSocket snapshot envelope.

## Canonical fields

| Field | Meaning / unit |
| --- | --- |
| `icao24` | Lowercase six-digit ICAO hex string, preserving leading zeros |
| `callsign` | Trimmed non-empty source text, otherwise null; not a flight identity |
| `latitude_deg`, `longitude_deg` | WGS84 decimal degrees, finite, −90…90 and −180…180 |
| `barometric_altitude_m` | Pressure altitude in metres; not height above ground |
| `geometric_altitude_m` | Geometric altitude in metres; retain source vertical datum |
| `geometric_altitude_reference` | `wgs84_ellipsoid` when geometric altitude exists, otherwise null |
| `groundspeed_mps` | Nonnegative metres/second |
| `ground_track_deg` | Direction of motion, clockwise from true north, [0, 360) |
| `true_heading_deg`, `magnetic_heading_deg` | Separate headings with declared reference |
| `barometric_vertical_rate_mps`, `geometric_vertical_rate_mps` | Separate vertical rates; positive upward |
| `roll_deg`, `pitch_deg` | Nullable orientation; source-supported values only |
| `aircraft_type` | Nullable aircraft type designator, not ADS-B reception type |
| `on_ground` | Nullable boolean; unknown is not false |
| `position_observed_at` | Aware UTC time of the position, nullable if unresolvable |
| `last_message_at` | Aware UTC time of latest aircraft message; may be newer than position |
| `provider_timestamp`, `received_at` | Separate provider snapshot and local receipt times |
| `source`, `source_type` | Provider identifier and source reception/provenance information |
| `mlat_fields`, `tisb_fields` | Provider-reported field names with MLAT/TIS-B provenance; preserved, not inferred |

Use timezone-aware Python datetimes and RFC 3339 UTC JSON strings. Never output
NaN or infinity. Latitude and longitude must occur together; absent positions
also require their position time. Rows without a resolvable age retain metadata
but discard coordinates and position time; they cannot become globe entities.
Unknown is null, not zero.
Handle source duplicates deterministically by position time without rejuvenating
old coordinates when a newer non-position message arrives. The cache retains
the whole prior observation in that case; it does not mix timestamps/metadata.

## ADSB.lol mapping

The [upstream field documentation](https://github.com/wiedehopf/readsb/blob/dev/README-json.md)
defines feet for altitudes, knots for speed, feet/minute for rates, and degrees
for track/headings. Geometric altitude references the WGS84 ellipsoid. `seen_pos`
and `seen` are ages in seconds. Its `hex` can carry a `~` prefix for non-ICAO
identities; do not strip this and pretend it is an ICAO address. The adapter skips
these records with a `non_icao_address` count. An explicit address-kind model
would be needed before supporting them. `alt_baro="ground"` indicates ground status, not
an altitude of zero. `t` is aircraft type; `type` is reception/source type.

Exact conversion constants:

```text
feet → metres:             value × 0.3048
knots → metres/second:     value × 1852 / 3600
feet/minute → metres/sec:  value × 0.3048 / 60
nautical miles → metres:   value × 1852
```

The v2 `now` field is interpreted explicitly as epoch milliseconds. Published
`jv2` source and the Phase 2 live capture agree; see
[provider research](provider-research.md). Position epoch seconds are
`now / 1000 - seen_pos`, not minus `seen`. Receipt time is separate.
Malformed, stale or implausibly future envelopes fail the batch. Invalid
individual rows are isolated and counted as `invalid_record`. A completely
rejected non-empty batch is not a healthy empty sky. Committed fixtures are
synthetic; real captures remain local with source, retrieval time and license.

## Batch and service snapshots

`ProviderBatch` contains an immutable observation tuple, source attribution,
snapshot/receipt times, raw/rejected counts, rejection reasons and selected HTTP
metadata. Counts must reconcile and a batch cannot mix providers. The service
stores batch metadata and a separate bounded observation cache; duplicate IDs,
expiry and the cache limit can make the cached count smaller than accepted rows.

The CLI's `IngestionSnapshot` adds `emitted_at`, `source`, `status`
(`idle`, `healthy`, `degraded` or `suspended`), `last_error`, failure count,
`last_success_at`, `retry_in_seconds`, batch metadata, `aircraft` and
`stale_aircraft_count`. `retry_in_seconds` is null when suspended. A healthy
provider snapshot does not guarantee every position is fresh. After an outage,
batch metadata describes the last successful fetch while the status describes
the latest attempt. The public `SnapshotMessage` wraps this data with `type`,
`schema_version`, `stream_id`, `sequence`, `poller_status` and the `tracking`
configuration (area/poll interval/stale/expiry thresholds); see the [API
contract](api.md). Emission time and retry countdown are not measurements.
Reading an existing state does not increment its sequence; stale transitions,
expiry, source updates and provider/poller status changes do.

## Truth in visualization

Keep source-reported, provider-calculated, and display-inferred quantities
distinguishable. The upstream format says true heading may itself be derived
from magnetic heading; a field called `true_heading` is not proof of a direct
measurement. Do not use selected/autopilot `nav_heading` as actual heading.

For Cesium, convert `(longitude, latitude, ellipsoid height)` in that order.
The viewer uses geometric altitude only when its WGS84 datum is confirmed. It
does not substitute barometric altitude. With a position but no geometric
height, an outlined aircraft symbol is an explicit **surface projection**, with
that qualification in the legend/list/inspector. Its logical display coordinate
remains ellipsoid height zero; the renderer clamps the symbol and label to the
displayed terrain when enabled. This does not assert the aircraft is at zero
height, on actual terrain, or at
DEN's ground elevation. The underlying unknown altitude stays null. Missing
coordinates create no marker. Terrain relief is a separate approximate map layer;
no geoid correction or terrain-clearance calculation is provided. Filled
geometric-height markers are source observations, not evidence
of a directly measured body orientation. Do not invent pitch from
vertical rate or roll from turns. Record only source observations in history;
interpolated positions belong to the display timeline.

The viewer keeps the canonical observation unchanged and derives a separate
display state. With interpolation enabled, it delays the display timeline by the
poll interval plus five seconds, capped at 60 seconds. The browser retains at
most four position samples per ICAO24 and uses Cesium's WGS84
`EllipsoidGeodesic` between adjacent samples whose gap is no more than 60
seconds and whose horizontal jump passes the 1,500 m/s display guard. Geometric
height is linearly interpolated only when both endpoints have the same height
availability. Before a bracket, after the latest sample, across a rejected
segment or after the earlier endpoint becomes stale, the display holds or falls
back to an observed position; it never extrapolates indefinitely. Stale state
disables interpolation and expiry removes the record according to `tracking`.
The inspector labels the latest observation time and the display time
separately, so a smoothly rendered coordinate is not presented as a new source
measurement.

When enabled, the renderer adds a fixed 4 km ground-track cue from the latest
reported `ground_track_deg`. The cue is a visual direction indicator, not a
speed vector, predicted destination or aircraft-body orientation. The
synthetic fixture's two circular paths are fictional display tests; its
ground-track value comes from a one-second forward difference on that path, while
true/magnetic heading, pitch and roll remain unknown. The Phase 5 controls can
hide geometric height, labels or cues without changing the canonical model;
unknown height remains a surface projection and barometric altitude is never
used as a substitute.

## Weather and route context

Route and weather context are separate API models, not fields inferred from ADS-B.
A callsign match in VRS standing data can describe a published airport chain, but
does not establish this aircraft's current flight. Two-airport references remain
explicitly unconfirmed; multi-leg and missing routes leave current departure and
destination unknown. Historical playback never assigns a present-day route to a
past observation.

Weather is a timestamped NWS KDEN station report, independent of the globe camera.
Temperature/dewpoint use Celsius; wind uses degrees and metres/second; visibility
and first-layer cloud base use metres as reported. A clear-sky reporting limit is
not a cloud base or ceiling. Missing/rejected values remain null. Weather time,
fetch time and staleness are separate from aircraft times. See [API](api.md).

## Persistent observations

The optional `skytrace_history.aircraft_positions` table stores the complete
canonical observation in JSONB plus recorded source attribution in another JSONB
column. Indexed fields are source, ICAO24, position observation time, provider
time, receipt time, latitude/longitude and a `geography(Point,4326)` point.
`ST_MakePoint` receives longitude first, latitude second. Geometry is 2D;
barometric and geometric heights keep their explicit values and references in
the JSON, never silently become a Z coordinate with an unspecified datum.

The primary key is `(source, icao24, position_observed_at)`. First-write-wins
deduplication ignores repeated polls of the same position timestamp, including
later metadata corrections at that exact key. Records with no position time
are excluded. Source data is recorded once per successful provider batch, before
display interpolation and independently of browser connections. Canonical model
validation is applied again when reading stored JSON.

Queries are source-qualified, UTC and half-open `[start, end)`, bounded to
24 hours and 5,000 records. The returned attribution comes from the first
selected row; attribution is also retained on every stored record. No source
mixing is permitted within one query. ICAO24 identifies the airframe/address;
neither it nor callsign establishes a flight identifier or route/destination.
Replay chooses the observation preceding its virtual time, keeps it separate
from interpolated display coordinates, and never stores the display result.
