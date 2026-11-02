import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  geoidUndulation,
  meshAlignedExportRectangle,
  navd88ToNad83Ellipsoid,
  TERRAIN_DETAIL_MAX_LEVEL,
  TERRAIN_MAX_LEVEL,
  TERRAIN_SAMPLES,
  terrariumHeights,
} from "./elevation.ts";
import { MAP_DEFAULTS, parseMapPreferences } from "./preferences.ts";
import { OpenTerrainProvider } from "./terrain.ts";

test("terrain RGB decoding preserves fractional and negative elevation", () => {
  const pixels = new Uint8ClampedArray([
    137,
    219,
    68,
    255, // documented Mapzen sample: 2523.265625 m
    128,
    0,
    0,
    255, // zero
    127,
    255,
    128,
    255, // -0.5 m
    128,
    1,
    0,
    255, // 1 m
  ]);
  assert.deepEqual(
    [...terrariumHeights(pixels, 2, 2, 2)],
    [2523.265625, 0, -0.5, 1],
  );
  assert.equal(
    terrariumHeights(pixels, 2, 2, 3)[4],
    (2523.265625 - 0.5 + 1) / 4,
  );
  assert.equal(TERRAIN_MAX_LEVEL, 15);
  assert.equal(TERRAIN_SAMPLES, 129);
  assert.equal(TERRAIN_DETAIL_MAX_LEVEL, 19);
});

test("NGS geoid grid converts NAVD88 without hiding its bounded coverage", () => {
  const undulation = geoidUndulation(-104.9903, 39.7392);
  assert.ok(undulation !== undefined);
  // The direct NGS GEOID18 response at this point was -17.111 m. The bundled
  // 0.1-degree grid adds interpolation error and is not a survey correction.
  assert.ok(Math.abs(undulation + 17.111) < 0.04);
  assert.equal(
    navd88ToNad83Ellipsoid(-104.9903, 39.7392, 1600),
    1600 + undulation,
  );
  assert.equal(geoidUndulation(-106, 39.7), undefined);
  assert.equal(navd88ToNad83Ellipsoid(-104.9, 39.7, Number.NaN), undefined);
});

test("129-node 3DEP exports put every shared tile edge node at the same source coordinate", () => {
  const west = meshAlignedExportRectangle({
    west: 0,
    south: 0,
    east: 64,
    north: 64,
  });
  const east = meshAlignedExportRectangle({
    west: 64,
    south: 0,
    east: 128,
    north: 64,
  });
  const pixelCenter = (low: number, high: number, index: number): number =>
    low + ((index + 0.5) * (high - low)) / TERRAIN_SAMPLES;
  // The outer column of the west tile and inner column of the east tile both
  // sample x=64. Matching every row proves the whole shared edge is aligned.
  assert.equal(pixelCenter(west.west, west.east, TERRAIN_SAMPLES - 1), 64);
  assert.equal(pixelCenter(east.west, east.east, 0), 64);
  for (let row = 0; row < TERRAIN_SAMPLES; row++)
    assert.equal(
      pixelCenter(west.south, west.north, row),
      pixelCenter(east.south, east.north, row),
    );
  assert.throws(
    () => meshAlignedExportRectangle({ west: 1, south: 0, east: 0, north: 1 }),
    /Invalid/,
  );
});

test("3DEP detail availability excludes a tile whose geoid center is outside the grid", () => {
  const provider = new OpenTerrainProvider();

  // At level 19 this tile overlaps the inclusive west edge (-105.8 degrees)
  // by a narrow sliver, but its center is -105.800056 degrees. A detail
  // request for it cannot receive the bounded GEOID18 correction and must not
  // be advertised as available.
  assert.equal(provider.getTileDataAvailable(108061, 198294, 19), false);
  provider.stop();
});

test("missing terrain pixels and invalid dimensions do not become fake zero heights", () => {
  assert.throws(
    () => terrariumHeights(new Uint8ClampedArray(16), 2, 2),
    /missing/,
  );
  assert.throws(
    () => terrariumHeights(new Uint8ClampedArray(12), 2, 2),
    /dimensions/,
  );
});

test("map settings survive corruption and validate each field", () => {
  assert.deepEqual(parseMapPreferences(null), MAP_DEFAULTS);
  assert.deepEqual(parseMapPreferences("{broken"), MAP_DEFAULTS);
  assert.deepEqual(parseMapPreferences("null"), MAP_DEFAULTS);
  assert.deepEqual(
    parseMapPreferences(
      '{"imagery":"unknown","terrain":"yes","airport":false}',
    ),
    {
      imagery: "aerial",
      terrain: true,
      airport: false,
      places: true,
      landmarks: true,
    },
  );
  assert.deepEqual(
    parseMapPreferences('{"imagery":"offline","terrain":false,"airport":true}'),
    {
      imagery: "offline",
      terrain: false,
      airport: true,
      places: true,
      landmarks: true,
    },
  );
});

test("bundled FAA snapshot matches its provenance and contains all six DEN runways", () => {
  const bytes = readFileSync(
    new URL("../../public/airports/den.geojson", import.meta.url),
  );
  const manifest = JSON.parse(
    readFileSync(
      new URL("../../public/airports/den-manifest.json", import.meta.url),
      "utf8",
    ),
  );
  const data = JSON.parse(bytes.toString());
  assert.equal(
    createHash("sha256").update(bytes).digest("hex"),
    manifest.sha256,
  );
  assert.equal(data.features.length, manifest.feature_count);
  assert.equal(
    data.features.filter(
      (f: { properties: { kind: string } }) => f.properties.kind === "runway",
    ).length,
    6,
  );
  const ids = new Set();
  const visit = (value: number[] | unknown[]): void => {
    if (typeof value[0] === "number") {
      const [lon, lat] = value as number[];
      assert.ok(
        lon !== undefined &&
          lat !== undefined &&
          lon > -104.76 &&
          lon < -104.6 &&
          lat > 39.79 &&
          lat < 39.94,
      );
    } else for (const child of value) visit(child as number[]);
  };
  for (const feature of data.features) {
    assert.equal(feature.properties.airport, "KDEN");
    assert.ok(!ids.has(feature.id));
    ids.add(feature.id);
    visit(feature.geometry.coordinates);
  }
});
