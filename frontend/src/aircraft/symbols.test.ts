import assert from "node:assert/strict";
import test from "node:test";
import { symbolDirection, symbolImage, symbolKind } from "./symbols.ts";

test("known types have distinct silhouettes; unfamiliar types stay generic", () => {
  assert.equal(symbolKind("B738"), "airliner");
  assert.equal(symbolKind(" b77w "), "widebody");
  assert.equal(symbolKind("C172"), "prop");
  assert.equal(symbolKind("GLF6"), "business");
  assert.equal(symbolKind("R44"), "helicopter");
  assert.equal(symbolKind(null), "airplane");
  assert.equal(symbolKind("B999"), "airplane");
  assert.equal(symbolKind("<script>"), "airplane");
  assert.notEqual(symbolImage("prop"), symbolImage("helicopter"));
});

test("symbol direction uses true heading before track, never invents a bearing", () => {
  assert.deepEqual(
    symbolDirection({ true_heading_deg: 0, ground_track_deg: 90 }),
    { degrees: 0, label: "Reported true heading" },
  );
  assert.equal(
    symbolDirection({ true_heading_deg: null, ground_track_deg: 0 }).degrees,
    0,
  );
  assert.match(
    symbolDirection({ true_heading_deg: null, ground_track_deg: 180 }).label,
    /not heading/,
  );
  assert.equal(
    symbolDirection({ true_heading_deg: null, ground_track_deg: null }).degrees,
    null,
  );
});

test("surface, stale, and unknown-direction symbols remain visually distinct", () => {
  const variants = [
    symbolImage("airliner"),
    symbolImage("airliner", true),
    symbolImage("airliner", false, true),
    symbolImage("airliner", false, false, true),
  ];
  assert.equal(new Set(variants).size, 4);
  assert.ok(variants.every((image) => image.startsWith("data:image/svg+xml,")));
  assert.match(decodeURIComponent(variants[3] ?? ""), /<circle/);
});
