import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "../network/fixture.ts";
import { parseHistory } from "../network/history.ts";
import type { Aircraft } from "../network/snapshot.ts";
import { HistoricalReplay } from "./replay.ts";

const epoch = Date.parse("2026-10-19T12:00:00Z");
export function historyFixture(gap = 30) {
  const base = fixture().data.aircraft[0] as Aircraft;
  return {
    schema_version: 1,
    source: "synthetic",
    icao24: base.icao24,
    start: "2026-10-19T11:00:00Z",
    end: "2026-10-19T13:00:00Z",
    truncated: false,
    attribution: {
      name: "synthetic",
      attribution: "Fictional test data",
      url: null,
      license: "MIT",
      license_url: null,
    },
    observations: [0, gap].map((seconds) => ({
      ...base,
      latitude_deg: 0,
      longitude_deg: seconds / 1000,
      geometric_altitude_m: 3000 + seconds * 10,
      geometric_altitude_reference: "wgs84_ellipsoid",
      position_observed_at: new Date(epoch + seconds * 1000).toISOString(),
      provider_timestamp: new Date(epoch + seconds * 1000).toISOString(),
      last_message_at: new Date(epoch + seconds * 1000).toISOString(),
      received_at: new Date(epoch + seconds * 1000).toISOString(),
    })),
  };
}

test("replay starts at recorded data, interpolates on its own clock, and stops at the final observation", () => {
  const history = parseHistory(historyFixture());
  const replay = new HistoricalReplay();
  replay.load(history);
  assert.deepEqual(replay.timeline, { start: epoch, end: epoch + 30_000 });
  replay.advance(15_000);
  const plane = replay.current(true)[0];
  assert.ok(plane);
  assert.equal(plane.display.mode, "interpolated");
  assert.equal(plane.display.timestamp, epoch + 15_000);
  assert.equal(plane.display.position?.height, 3150);
  assert.equal(plane.observation.geometric_altitude_m, 3000);
  assert.equal(history.observations[1]?.geometric_altitude_m, 3300);
  replay.advance(1_000_000);
  assert.equal(replay.currentTime, epoch + 30_000);
  assert.equal(replay.current(true)[0]?.display.mode, "held");
  replay.setTime(epoch - 50_000);
  assert.equal(replay.currentTime, epoch);
  replay.clear();
  assert.equal(replay.loaded, false);
  assert.deepEqual(replay.current(true), []);
});

test("replay marks long gaps and never invents heights across missing altitude", () => {
  const replay = new HistoricalReplay();
  replay.load(parseHistory(historyFixture(91)));
  replay.advance(70_000);
  assert.equal(replay.current(true)[0]?.display.mode, "held");
  assert.equal(replay.current(true)[0]?.stale, true);
  const missing = historyFixture();
  const second = missing.observations[1] as Record<string, unknown>;
  second.geometric_altitude_m = null;
  second.geometric_altitude_reference = null;
  replay.load(parseHistory(missing));
  replay.advance(15_000);
  assert.match(
    replay.current(true)[0]?.display.reason ?? "",
    /Altitude availability changed/,
  );
  assert.equal(replay.current(true)[0]?.display.position?.height, 3000);
  assert.equal(replay.current(false)[0]?.display.position?.height, 0);
  assert.equal(replay.current(false)[0]?.display.position?.projected, true);
});
