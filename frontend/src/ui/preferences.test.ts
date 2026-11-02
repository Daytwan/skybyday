import assert from "node:assert/strict";
import test from "node:test";
import {
  DISPLAY_DEFAULTS,
  heightLabel,
  parseDisplayPreferences,
  speedLabel,
} from "./preferences.ts";

test("display preferences reject malformed and mistyped values independently", () => {
  for (const raw of [null, "{", "false", "[]"])
    assert.deepEqual(parseDisplayPreferences(raw), DISPLAY_DEFAULTS);
  assert.deepEqual(
    parseDisplayPreferences(
      '{"units":"metric","labels":"all","altitude":false,"interpolate":"false","tracks":1}',
    ),
    { ...DISPLAY_DEFAULTS, units: "metric", labels: "all", altitude: false },
  );
  assert.equal(
    parseDisplayPreferences('{"units":"mph","labels":"everything"}').units,
    "aviation",
  );
});
test("aviation display conversions retain unknowns, zero and negative altitude", () => {
  assert.equal(heightLabel(3048, "aviation"), "10,000 ft");
  assert.equal(heightLabel(-30.48, "aviation"), "-100 ft");
  assert.equal(heightLabel(0, "aviation"), "0 ft");
  assert.equal(heightLabel(null, "aviation"), "Unknown");
  assert.equal(speedLabel((1852 / 3600) * 250, "aviation"), "250 kt");
  assert.equal(speedLabel(null, "aviation"), "Unknown");
  assert.equal(speedLabel(0, "aviation"), "0 kt");
  assert.equal(heightLabel(3048, "metric"), "3,048 m");
  assert.equal(speedLabel(123.45, "metric"), "123.5 m/s");
});
