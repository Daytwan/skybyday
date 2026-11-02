# Denver skyline sources and limits

SkybyDay's Denver building layer uses measured public data in two deliberately
bounded forms. It does not claim a photorealistic city, current construction
status, engineering accuracy, or detailed buildings outside the published
coverage below.

## City building volumes

The primary layer is the City and County of Denver's official
[Building Outlines 2022](https://www.arcgis.com/home/item.html?id=04e5069b4cf843d2bfe15ad0d1c66e00)
feature service, layer 111. The source describes roofprints stereo-compiled from
spring 2022 DRCOG aerial imagery. It supplies a building identifier, one scalar
roof height, ground elevation and building type. The source coordinate system is
Colorado Central State Plane, EPSG:2877, in US survey feet. Horizontal and
vertical source datums are NAD83(2011) and NAVD88(GEOID12A).

The release snapshot was retrieved 21 October 2026. The service reports its data
last edited 1 October 2024. Two fixed envelopes are retained:

- downtown: 39.728–39.766° N, 105.013–104.967° W;
- DEN: 39.79–39.94° N, 104.76–104.60° W.

The builder paginates at the published 2,000-feature limit and retains a feature
only when its representative roof coordinate is inside an envelope. It validates
the exact service item/layer, EPSG, units, attributes, closed WGS84 rings, height
and ground-elevation ranges. The resulting snapshot contains 19,242 roof-level
records in 137 chunks, occupies about 9.9 MB, and records every chunk hash and
upstream page hash in
`frontend/public/landmarks/denver-buildings/manifest.json`. Runtime browsers make
no calls to the feature service.

Heights are converted with the exact US survey foot factor, 1200/3937 metres per
foot. Ground elevation uses the bundled NOAA NGS GEOID12A grid to apply
`h = H + N` before Cesium placement. The result is a NAD83 ellipsoid height, not
a complete NAD83(2011)-to-WGS84 frame/epoch transformation. That remaining frame
uncertainty, the source's single height per roof level, and its explicit
“not for engineering purposes” term prevent survey, clearance, or collision
claims. Buildings are accurate measured roofprint volumes with flat display tops;
they are not roof-plane models or facade scans.

The renderer requests only chunks intersecting the current close view, ranks them
around the centre-screen globe intersection, limits a view to 16 chunks, fetches
at most four concurrently, and retains at most 24. View bounds include terrain
intersections at nine viewport points as well as the ellipsoid rectangle, so
elevated foreground in an oblique view is included. Each chunk becomes a batched
Cesium primitive rather than hundreds of independently managed entities. Moving
away hides the volumes. Disabling the layer aborts pending municipal chunk
requests, clears queued chunks, and removes/destroys retained building primitives
and the I3S mesh. Cancelled requests retain their concurrency slots until they
settle, including during a rapid off/on. The manifest and peak data stay cached;
peak labels are hidden. Telemetry is independent.

Refresh manually from the repository root:

```bash
python frontend/scripts/build-denver-buildings.py
npm --prefix frontend test
npm --prefix frontend run build
```

Review source metadata, counts, hashes and rendered comparison scenes before
accepting a refresh. Do not schedule this script or run it per viewer.

## McNichols photogrammetry pilot

The one genuine detailed mesh is the City's public
[McNichols Building scene layer](https://www.arcgis.com/home/item.html?id=c2a3283542294d0b8f93c08230668a49).
Its metadata says five drone captures were processed in Pix4D and published as an
integrated I3S mesh. Coverage is only approximately 104.989967–104.989227° W and
39.739536–39.739933° N. It is a measured, textured pilot around one building and
adjacent landscape, not downtown-wide photogrammetry.

The service publishes WGS84 horizontal coordinates and EGM2008 orthometric
heights. Cesium loads it through `I3SDataProvider` with the public
[NGA EGM2008 geoid service](https://www.arcgis.com/home/item.html?id=d798c71512404bbb9c1551b827bf5467)
for orthometric-to-ellipsoid placement. SkybyDay begins loading only when the
enabled camera is below 12 km and the visible rectangle intersects that tiny
extent. I3S then streams visible levels of detail; it is not downloaded into the
repository or fetched at application startup. The tile cache and overflow are
each capped at 64 MiB and the screen-space error target is eight pixels.

If the scene or geoid service fails, municipal measured volumes and peak labels
continue. The source status explicitly names whether McNichols photogrammetry is
loading, visible, or unavailable. This layer carries the City's public as-is,
no-warranty, not-for-engineering use terms.

Municipal fallback geometry stays visible until the I3S tileset selects nonempty
triangle content for a frame. Fallback changes occur after rendering, outside tile
traversal, and restore when mesh content is absent or fails. A failed mesh attempt
waits for a deliberate layer off/on or offline/online transition before retrying.
Disabling remote access disposes the mesh; an already-started I3S initialization
cannot be cancelled through this API, so a late result is destroyed if still
disabled. No claim is made that every I3S network request stops immediately.

On 23 October 2026 the production browser at the Civic Center preset requested
real scene geometry/textures and visibly rendered the textured McNichols building.
Layer off/on restored it successfully. See the [map recovery record](map-recovery-2026-10-23.md)
for screenshots and test scope. This establishes visibility in that scene; it does
not quantify geospatial alignment or accept all camera views.

## Peaks and remaining gaps

Named Front Range peak labels remain from the bounded OpenStreetMap snapshot under
ODbL 1.0. The old 1,697 OSM building extrusions are hidden when this layer is used;
they are not stacked with the measured City volumes. Peaks are labels, not
elevation measurements.

Acceptance still requires rendered desktop and mobile comparisons at fixed DEN,
downtown, McNichols and Front Range cameras. Distinct roof forms beyond McNichols,
facade textures across downtown, interiors, monuments, current construction and
other cities remain unimplemented. More generic extrusions cannot close those
gaps; each expansion needs a documented geometry source, license, datum and LOD
budget.
