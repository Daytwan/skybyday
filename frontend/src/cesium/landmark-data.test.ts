import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  parseBuildingChunk,
  parseBuildingManifest,
  representativeCoordinate,
  visibleBuildingChunks,
} from "./landmark-data.ts";

test("municipal building snapshot hashes, counts, units, and geometry match", () => {
  const base = new URL(
    "../../public/landmarks/denver-buildings/",
    import.meta.url,
  );
  const rawManifest = JSON.parse(
    readFileSync(new URL("manifest.json", base), "utf8"),
  );
  const manifest = parseBuildingManifest(rawManifest);
  assert.equal(rawManifest.source_units, "US survey feet");
  assert.equal(rawManifest.conversion_metres_per_foot, 1200 / 3937);
  assert.match(rawManifest.height_semantics, /Highest roof point/);
  assert.match(rawManifest.limitations, /not roof planes/);
  let count = 0;
  for (const [key, entry] of Object.entries(manifest.chunks)) {
    const bytes = readFileSync(new URL(`${key}.json`, base));
    assert.equal(bytes.length, entry.bytes);
    assert.equal(
      createHash("sha256").update(bytes).digest("hex"),
      entry.sha256,
    );
    const buildings = parseBuildingChunk(JSON.parse(bytes.toString()));
    assert.equal(buildings.length, entry.count);
    for (const building of buildings) {
      const [lon, lat] = representativeCoordinate(building);
      assert.ok(lon >= entry.bounds[0] && lon <= entry.bounds[2]);
      assert.ok(lat >= entry.bounds[1] && lat <= entry.bounds[3]);
    }
    count += buildings.length;
  }
  assert.equal(count, manifest.building_count);
  assert.equal(count, 19_242);
});

test("visible building chunks are intersecting, nearest-first, and bounded", () => {
  const manifest = parseBuildingManifest({
    version: 1,
    building_count: 3,
    chunk_degrees: 0.01,
    vertical_reference: "NAVD88(GEOID12A) orthometric heights",
    chunks: {
      "1-1": {
        count: 1,
        bytes: 100,
        sha256: "a".repeat(64),
        bounds: [-105.01, 39.73, -105.0, 39.74],
      },
      "1-2": {
        count: 1,
        bytes: 100,
        sha256: "b".repeat(64),
        bounds: [-105.0, 39.73, -104.99, 39.74],
      },
      "9-9": {
        count: 1,
        bytes: 100,
        sha256: "c".repeat(64),
        bounds: [-104.7, 39.8, -104.69, 39.81],
      },
    },
  });
  assert.deepEqual(
    visibleBuildingChunks(
      manifest,
      { west: -105.02, south: 39.72, east: -104.98, north: 39.75 },
      [-104.992, 39.735],
      1,
    ),
    ["1-2"],
  );
  assert.deepEqual(
    visibleBuildingChunks(
      manifest,
      {
        west: -104.8,
        south: 39.7,
        east: -104.6,
        north: 39.9,
      },
      [-104.7, 39.8],
    ),
    ["9-9"],
  );
});

test("tilted wide views rank chunks around the globe focus, not horizon bounds", () => {
  const manifest = parseBuildingManifest({
    version: 1,
    building_count: 2,
    chunk_degrees: 0.01,
    vertical_reference: "NAVD88(GEOID12A) orthometric heights",
    chunks: {
      "1-1": {
        count: 1,
        bytes: 100,
        sha256: "d".repeat(64),
        bounds: [-104.995, 39.74, -104.985, 39.75],
      },
      "2-2": {
        count: 1,
        bytes: 100,
        sha256: "e".repeat(64),
        bounds: [-104.8, 39.95, -104.79, 39.96],
      },
    },
  });
  assert.deepEqual(
    visibleBuildingChunks(
      manifest,
      { west: -105.1, south: 39.6, east: -104.7, north: 40.0 },
      [-104.99, 39.745],
      1,
    ),
    ["1-1"],
  );
});

test("malformed or unbounded municipal data is rejected", () => {
  assert.throws(() => parseBuildingChunk({ version: 1, buildings: [[1]] }));
  assert.throws(() =>
    parseBuildingManifest({
      version: 1,
      building_count: 1,
      chunk_degrees: 1,
      vertical_reference: "unknown",
      chunks: {},
    }),
  );
});
