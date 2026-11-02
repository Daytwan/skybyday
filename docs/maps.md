# Map quality release

This release prioritizes useful map detail and navigation before expanding the
telemetry footprint. It adds no production dependency, account, database or API
key. Map requests are made by Cesium for the visible viewport and do not trigger
aircraft-provider requests. Tests use synthetic telemetry.

Updated 24 October 2026: current detail also includes bounded USGS 3DEP terrain,
municipal Denver roofprints and one McNichols mesh. See [terrain sources](terrain-sources.md),
[skyline sources](skyline-sources.md), and [current map verification](map-recovery-2026-10-23.md).
The 20 October source table below describes the original foundation.

The global `NASA Earth relief` choice uses NASA GIBS Blue Marble Shaded Relief
and Bathymetry WMTS in EPSG:3857 through level 8 (service and tile access
verified 24 October 2026). It is a no-account,
visible-viewport global visual base with a documented offline fallback. It is
not an assertion of current imagery, local aerial resolution, or worldwide
photorealism. The EPSG:3857 tiles stop at approximately ±85.0511° latitude;
the bundled background remains at the polar caps. USGS aerial remains the
separate US-focused choice.

World terrain now caps at level 15 and uses a 129×129 grid per tile. That is a
four-times-finer maximum display-grid spacing than the earlier 65×65,
level-14 configuration, while retaining a finite 16,641-sample (about 65 KiB)
decoded heightmap budget per tile. It does not upgrade heterogeneous source
resolution or vertical datum quality into measured global terrain.

## Sources verified 20 October 2026

| Layer | Source and access | Scope and limits |
| --- | --- | --- |
| Aerial | [USGS Imagery Only cached service](https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer), no key | Web Mercator XYZ cache, 256 px; requested through level 16, the service's advertised 1:9028 limit. About 1.8 m ground pixel spacing at DEN. High-detail US coverage; coarse imagery elsewhere. CONUS source photography listed as 2017–2021, service refreshed June 2024. Not live satellite photography. |
| Terrain | [Mapzen Terrain Tiles / AWS Open Data](https://registry.opendata.aws/terrain-tiles/), public bucket, no AWS account | [Terrarium PNG encoding](https://github.com/tilezen/joerd/blob/master/docs/formats.md), Web Mercator. Decode RGB to metres, then bilinearly sample heights into fixed 129 × 129 grids. Zoom capped at 15; no normals, water mask, terrain exaggeration or geoid conversion. Source resolution/datums vary. |
| Airport | [FAA ADDS airport features](https://adds-faa.opendata.arcgis.com/), [release notice](https://www.faa.gov/air_traffic/flight_info/aeronav/safety_alerts/media/ADDS_25-01_DPN_Open_Data_Release.pdf) | Bounded KDEN extract: 6 runway, 484 taxiway, 32 apron, 37 building polygons. Effective 03 Sep–29 Oct 2026, retrieved 20 Oct. Reference/sample use only. No live operational-status assertion. |
| Offline | [Natural Earth II](https://www.naturalearthdata.com/about/terms-of-use/), bundled with Cesium | Public-domain, low-resolution global background. Flat ellipsoid. Works without external map requests after selecting Offline world. |

The map's **Sources & dates** page contains attribution, licensing distinctions,
effective dates and source links. USGS's Alaska SPOT imagery has download
restrictions; the application only displays service tiles, honors browser
caching and has no imagery download/prefetch feature. No service reviewed
provides a fixed request allowance or an availability guarantee for this app.
On errors, stop that layer's attempt and show the fallback; manual **Retry maps**
starts a new attempt. Do not repeatedly press Retry during a provider outage.

The FAA refresh encountered an explicit service quota response during research.
The script stopped, and the subsequent attempt after the requested delay
succeeded. The application never calls the FAA service at runtime.

## Implementation and correctness

- `cesium/maps.ts` owns the local fallback, USGS imagery, terrain and airport
  layers. Generation checks prevent late initialization from overriding a newer
  user selection. Changing the airport overlay does not restart online layers.
- `cesium/terrain.ts` uses Cesium's scheduled `Resource` requests, finite detail,
  15-second deadlines and cancellation. Fine 3DEP failure retains world relief;
  base-terrain failure selects the labelled flat fallback. It does not build
  an unbounded tile cache or retry loop. Browser caching and Cesium tile retention
  remain in use.
- Terrain is **visual relief**, not surveyed ellipsoidal terrain. RGB sample
  centres are resampled over each tile extent; source seams and datum differences
  may remain. Detailed relief does not extend to polar caps outside Web Mercator.
  Aircraft geometric heights retain WGS84 ellipsoid semantics; pressure altitude
  never substitutes for geometric height. No AGL, collision or clearance claim.
- Unknown-height aircraft, airport geometry and area labels clamp to the displayed
  surface. The observation model is unchanged. Glyphs and labels remain readable
  even when terrain would otherwise obscure their annotation.
- `aircraft/symbols.ts` owns original SVG silhouettes and a small exact type-code
  lookup. Aircraft dimensions are not inferred. Unlisted/null types use a generic
  symbol. True heading takes precedence; ground track is a labelled fallback, and
  unknown direction uses a ringed, screen-oriented glyph. No magnetic-to-true
  conversion, pitch or roll is invented. Rotation changes only on new observations.
- `DEN airport` and `Front Range` are fixed reference camera views. They do not
  change the backend's configured tracking area. `North ↑` resets camera heading,
  pitch and roll. Reduced-motion users receive immediate camera transitions.
- Map preferences are versioned in browser local storage. Corrupt or unavailable
  storage cannot prevent startup. Offline world disables all external map layers,
  independent of whether aircraft telemetry is synthetic or live.

## Refreshing the bundled airport snapshot

Run from the repository root, manually:

```bash
python frontend/scripts/refresh-den-airport.py
npm --prefix frontend run check
npm --prefix frontend run build
```

The standard-library script checks the exact official service identities,
public access, query size/truncation, KDEN identifier and coordinate envelope.
It filters KDEN, selects attributes and rounds coordinates to seven decimals,
without topology simplification. No height is added. Runway labels prefer the
FAA designator and fall back to `RWY_ID`; unnamed terminal/gate labels are not
invented. Sources, query URLs, original descriptions, terms and SHA-256 are saved
in `frontend/public/airports/den-manifest.json`.

Review effective dates, counts and geometry differences before release. Update
`frontend/public/map-attribution.html` and these notes for the new cycle. A 429
stops the script: honor the service's retry delay before another attempt.
Do not run the refresh per page load or per connected client.

The FAA data retains its source terms. It is not relicensed under the project's
MIT code license. Imagery/terrain licenses likewise remain separate; see the
[upstream terrain notices](https://github.com/tilezen/joerd/blob/master/docs/attribution.md).

## Remaining quality limits

This is a substantial DEN-area improvement, not uniform global high-resolution
imagery or a world airport database. Full global aerial detail requires a provider
with explicit suitable access/redistribution terms or independently hosted data.
Terrain is bounded approximate relief; terminal interiors, gate assignments and
3D airport buildings are not included. The renderer still needs measurements
with larger traffic cohorts before global-scale performance can be claimed.

## Local experience update — 21 October 2026 UTC

**NAIP:** the account-free [USGS NAIP ImageServer](https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer)
provides a separate fine-detail layer over the DEN airport envelope
(39.79–39.94° N, 104.76–104.60° W). Visible export-image tiles use Web Mercator,
PNG32, bilinear resampling and levels 10–18. Level 18 is about 0.46 m ground pixel
spacing at Denver, not a promise of 0.46 m source resolution. The service describes
primarily 0.6 m NAIP imagery; acquisition dates and native resolution vary. Its
January 2025 service notice is not the photograph acquisition date. No API key,
account, bulk imagery download or private tile proxy is used. General imagery
remains beneath this layer; failures disable only the failed layer until an
explicit retry. This release does not extend dynamic NAIP exports nationwide.

**City names:** [GeoNames cities500](https://download.geonames.org/export/dump/readme.txt),
CC BY 4.0. The bundled extract has 236,560 places in 302 geographic chunks, plus a
small world-scale major-city index. Names appear progressively as the camera
moves and zooms. At most 180 labels are drawn, overlaps are suppressed, at most 16
chunks are considered per view, and a 12-chunk LRU bounds memory. All runtime
requests are to local static files; there are no GeoNames account/API requests.
This dataset includes settlements over 500 people or administrative seats down
to PPLA4; it is not literally every settlement or an assertion of official names,
current population, or perfect completeness. Population is used only for label
priority. Coordinates are WGS84 and rendered on the terrain.

Refresh from a manually downloaded ZIP using
`python frontend/scripts/build-places.py /path/to/cities500.zip`. The script checks
expanded archive size, coordinates and retains source hashes/counts in
`frontend/public/places/manifest.json`. Review changed licenses and counts before
replacing a snapshot. Update attribution and verification after refreshing.

**Legacy Denver skyline / current peak labels:** a one-time bounded OpenStreetMap Overpass export
contains 1,697 downtown building footprints with explicit `height` tags and 247
named Front Range peaks. © OpenStreetMap contributors, ODbL 1.0. The derived
[GeoJSON](../frontend/public/landmarks/denver.geojson) and manifest are distributed
under those data terms. The code's MIT license does not relicense them. The
builder script documents the exact query and accepts a saved response; it never
queries Overpass on a page load. [Public Overpass usage guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html)
discourages using the public instances as an application's ongoing backend.

Those OSM buildings are hidden in the current layer; measured municipal roofprints
replace them, with a textured McNichols pilot. See [skyline sources](skyline-sources.md)
for geometry, height references and limits. The legacy OSM heights came from
contributors, not verified surveys; storey counts were never converted into
assumed heights. Peaks remain named
reference points; their source elevation tags are not presented as measurements.
The one-building pilot does not establish photorealistic city or universal landmark
coverage. Detailed global landmark models remain future work.

## Weather and route sources

[NWS API documentation](https://www.weather.gov/documentation/services-web-api)
allows open use without fees, requires an identifying User-Agent, and keeps its
rate threshold unpublished. SkybyDay fetches the latest KDEN observation on
demand through one shared ten-minute cache, validates station/time/units, and
honors Retry-After plus bounded backoff. It preserves old data with an explicit
status and marks reports older than 90 minutes stale. Clear-sky reporting limits
are not displayed as measured cloud bases. This is station weather, not global
radar, terrain weather, a forecast, or aviation clearance information.

Published callsign routes come from the **ADSB.lol VRS standing-data mirror**,
whose [upstream repository](https://github.com/vradarserver/standing-data) is
[CC0](https://github.com/vradarserver/standing-data/blob/master/LICENSE).
ADSB.lol publishes its data under ODbL. Lookups are made only for selected,
currently cached live aircraft; the mirror URL convention is documented in the
[provider implementation](https://github.com/adsblol/api/blob/main/src/adsb_api/utils/api_routes.py).
No arbitrary URLs/callsigns are proxied. A process-wide 10-second minimum request
gap, 256-entry LRU, six-hour successful cache, negative caching, finite deadlines
and Retry-After protect the source. The callsign mirror is separate from the
telemetry API and its shared 30-second poller.

These are dated/undated database references, not dispatch-confirmed current
flights. Only two-airport entries populate departure and destination candidates;
longer chains are shown intact with the current leg unknown. No route is inferred
from coordinates, direction or nearby airports, and no current reference is
applied to replay. GA and unmatched callsigns remain Unknown. The ADSB.lol batch
routeset endpoint returned an empty body during verification, so this release
uses its standing-data mirror. ADSBdb was evaluated but not integrated: its
upstream README explicitly restricts copying/publishing route data without
permission. Do not substitute that source merely because its API is public.
