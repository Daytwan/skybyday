# Terrain sources and vertical reference

SkybyDay renders terrain for visual orientation and map context. It is not a
survey, navigation database, obstacle-clearance tool, or source of engineering
elevations. The application keeps the source datum and its conversion limits
visible rather than presenting a blended height as a measured value.

## Runtime sources

| Layer | Runtime source and credit | Request envelope | Resolution and limits |
| --- | --- | --- | --- |
| World terrain | [Mapzen Terrain Tiles on AWS Open Data](https://registry.opendata.aws/terrain-tiles/) using the [Terrarium format](https://github.com/tilezen/joerd/blob/master/docs/formats.md) | `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`; levels 0–15; Web Mercator; one visible Cesium tile at a time | A 256×256 Terrarium tile is decoded and resampled to a fixed 129×129 height grid (16,641 samples, about 65 KiB) for terrain stitching. Source resolution, acquisition date, vertical datum, and accuracy vary by region. No geoid conversion is applied. |
| Denver / Front Range detail | [USGS 3DEP ImageServer](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer) | `exportImage`, Web Mercator (`bboxSR=3857`, `imageSR=3857`), 129×129 `F32` LERC; levels 15–19; geographic envelope `west=-105.8`, `south=39.4`, `east=-104.4`, `north=40.1` | The 129×129 mesh is aligned to shared tile edges. At Denver level 19 its node spacing is approximately 0.45 m; that is display mesh spacing, not a claim about native 3DEP resolution or vertical accuracy. Collection dates and native resolution vary. Responses are bounded to 2 MiB and 15 seconds. |

The detail rectangle is deliberately bounded. `frontend/src/cesium/elevation.ts`
and `frontend/src/cesium/terrain.ts` are the runtime source of truth; the
manifest below records the geoid samples used for the conversion. A detail
failure leaves the coarser world terrain available. The runtime does not
prefetch or bulk-download terrain.

The current global mesh is a deliberate finite increase from the prior
65×65-at-level-14 ceiling: level 15 halves the tile span and 129 nodes double
the nodes per side, giving four times finer maximum-grid spacing. At the equator
the level-15 grid spacing is about 9.55 m; near Denver it is about 7.3 m in
ground distance. These are display-node spacings, not source resolution or
accuracy figures. Each decoded tile has 16,641 float samples (about 65 KiB),
while the downloaded Terrarium PNG remains 256×256 and browser/Cesium caching
continues to govern visible-tile retention.

The 23 October fix preserves the world provider when Cesium reports a failed
level 15–19 detail tile, allowing parent-tile upsampling. Base-terrain failure
still selects the explicitly labelled flat fallback. Targeted regression and an
actual browser check with controlled 3DEP HTTP 503 responses are recorded in
[map recovery verification](map-recovery-2026-10-23.md).

The Cesium terrain provider currently carries the visible credit
`Mapzen · USGS 3DEP · NOAA geoid`, linked to [`/map-attribution.html`](/map-attribution.html),
which includes the USGS 3DEP, NOAA NGS, Mapzen, and Tilezen source notices. The
source page also links [Tilezen's attribution notices](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)
and its [elevation source list](https://github.com/tilezen/joerd/blob/master/docs/data-sources.md).
The application does not relicense upstream data under its software license.

## Vertical datum and frame

USGS 3DEP values used by the detail path are NAVD88 orthometric heights. The
bundled [NOAA NGS GEOID18 sample manifest](../frontend/public/terrain/geoid-grid-manifest.json)
records the relation:

```text
h_NAD83(2011) = H_NAVD88 + N_geoid
```

The grid is sourced from the [NGS geoid service](https://geodesy.noaa.gov/api/geoid/ght)
and its [service documentation](https://geodesy.noaa.gov/web_services/geoid.shtml).
It covers exactly the same rectangle as the 3DEP detail envelope, at 0.1°
spacing, with 15 columns and 8 rows. Runtime bilinear interpolation is used
between samples. The manifest reports the NGS service's maximum errors (0.045 m
for GEOID18 and 0.002 m for GEOID12A); it does not quantify the additional
interpolation error.

GEOID18 is defined for NAD83(2011). Cesium's globe is rendered in a WGS84 frame,
and SkybyDay applies no NAD83-to-WGS84 frame or epoch transformation. The
resulting alignment is an explicit approximation. The code's
`HeightmapTerrainData.structure.heightOffset` applies the GEOID18 value to the
3DEP height; it does not imply survey-grade absolute positioning.

The Denver building snapshot has a separate source reference of
`NAVD88(GEOID12A)` and is converted through its own bundled grid. GEOID12A is
retained only for data explicitly tagged with that model; it must not be
silently substituted for GEOID18 terrain.

The world Terrarium fallback has no geoid correction. Aircraft geometric
altitudes retain their telemetry semantics and are not converted into AGL or
terrain clearance.

## Envelope and boundary behavior

The GEOID grid bounds are `[-105.8, 39.4, -104.4, 40.1]` in
`[west, south, east, north]` degrees. `geoidUndulation()` accepts the grid
edges with a small floating-point tolerance and returns `undefined` outside
the grid. Detail requests therefore need a tile-center correction that is
inside this envelope.

The terrain selector uses geographic rectangle intersection to decide whether a
level 15–19 tile is eligible. A tile can overlap the inclusive edge by a thin
sliver while its center is outside the geoid grid. Such a tile cannot be
corrected by the bundled samples and must not be treated as available detail;
the focused regression is in
[`frontend/src/cesium/maps.test.ts`](../frontend/src/cesium/maps.test.ts).

## Verification and operational limits

- The geoid manifest was generated 21 October 2026 UTC by
  `frontend/scripts/refresh-geoid-grid.py`. It records the source URLs, model
  identifiers, bounds, spacing, units, request count, and TypeScript source
  hash.
- The refresh script makes 240 bounded, sequential NGS requests (15×8 points
  for GEOID12A and GEOID18), with a short delay and finite retries. It is a
  maintainer operation, never a browser or per-viewer request.
- 3DEP requests use the public ImageServer export endpoint from the browser;
  no account or application credential is configured in this project. The
  service's availability, native resolution, and collection date can vary.
- Mapzen/Tilezen data carries its own upstream terms and attribution. The
  [map attribution page](/map-attribution.html) is the user-visible credit
  surface and should be reviewed whenever a source or bundled manifest changes.
- Outside the 3DEP envelope, only the lower-resolution world fallback is
  expected. A world globe therefore does not mean uniformly high-resolution
  terrain coverage.

