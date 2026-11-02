import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { inBounds, parsePlaces, placeChunks } from "./places-data.ts";

test("place chunks stay bounded and include both sides of the antimeridian", () => {
  assert.deepEqual(placeChunks({ west: 179, east: -179, south: 1, north: 2 }), [
    "9-35",
    "9-0",
  ]);
  assert.deepEqual(
    placeChunks({ west: -100, east: 100, south: -80, north: 80 }),
    [],
  );
  assert.ok(
    inBounds([1, "Example", -179, 1, 1000, false], {
      west: 179,
      east: -178,
      south: 0,
      north: 2,
    }),
  );
  assert.equal(
    parsePlaces([
      [1, "Valid", 0, 0, 0, false],
      [2, "Invalid", 181, 0, 0, false],
    ]).length,
    1,
  );
});

test("bundled global gazetteer hashes, counts, and coordinates match the manifest", () => {
  const base = new URL("../../public/places/", import.meta.url);
  const manifest = JSON.parse(
    readFileSync(new URL("manifest.json", base), "utf8"),
  );
  let total = 0;
  for (const [key, entry] of Object.entries(manifest.chunks) as [
    string,
    { count: number; sha256: string },
  ][]) {
    const bytes = readFileSync(new URL(`${key}.json`, base));
    assert.equal(
      createHash("sha256").update(bytes).digest("hex"),
      entry.sha256,
    );
    const places = parsePlaces(JSON.parse(bytes.toString()));
    assert.equal(places.length, entry.count);
    total += places.length;
  }
  assert.equal(total, manifest.count);
  assert.ok(total > 200_000);
});

test("Denver geometry uses explicit height measurements and published provenance", () => {
  const data = readFileSync(
    new URL("../../public/landmarks/denver.geojson", import.meta.url),
  );
  const manifest = JSON.parse(
    readFileSync(
      new URL("../../public/landmarks/denver-manifest.json", import.meta.url),
      "utf8",
    ),
  );
  assert.equal(
    createHash("sha256").update(data).digest("hex"),
    manifest.sha256,
  );
  const features = JSON.parse(data.toString()).features;
  assert.equal(features.length, manifest.buildings + manifest.peaks);
  for (const feature of features) {
    if (feature.properties.kind !== "building") continue;
    assert.ok(
      feature.properties.height_m >= 1 && feature.properties.height_m <= 400,
    );
    const ring = feature.geometry.coordinates[0];
    assert.deepEqual(ring[0], ring.at(-1));
  }
});
