import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "../network/fixture.ts";
import type { Aircraft } from "../network/snapshot.ts";
import { filterAircraft } from "./filters.ts";
import { trackEndpoint } from "./motion.ts";
import { AircraftState } from "./state.ts";

const epoch = Date.parse("2026-10-19T12:00:00Z");
const smooth = { interpolate: true, showAltitude: true };
function snapshot(seconds: number, patch: Partial<Aircraft> = {}) {
  const result = fixture();
  const time = new Date(epoch + seconds * 1000).toISOString();
  result.sequence = seconds + 1;
  result.data.emitted_at = time;
  result.data.last_success_at = time;
  result.data.aircraft = [
    {
      ...(result.data.aircraft[0] as Aircraft),
      latitude_deg: 0,
      longitude_deg: seconds / 1000,
      geometric_altitude_m: 3000 + seconds * 10,
      position_observed_at: time,
      provider_timestamp: time,
      received_at: time,
      last_message_at: time,
      ...patch,
    },
  ];
  return result;
}
function first(state: AircraftState, time: number, options = smooth) {
  const plane = state.current(time * 1000, options)[0];
  assert.ok(plane);
  return plane;
}

test("sampled hubs interpolate observed antimeridian crossings on their honest delayed cadence", () => {
  const state = new AircraftState();
  const a = snapshot(0, { longitude_deg: 179.9 });
  const b = snapshot(180, { longitude_deg: -179.9 });
  for (const item of [a, b]) {
    item.tracking.coverage_kind = "sampled_hubs";
    item.tracking.coverage_cycle_seconds = 180;
    item.tracking.stale_after_seconds = 420;
    item.tracking.expire_after_seconds = 900;
  }
  state.apply(a, 0);
  state.apply(b, 180_000);
  assert.equal(state.delaySeconds, 185);
  const crossing = first(state, 275); // source timeline 90 sec between observations
  assert.equal(crossing.display.mode, "interpolated");
  assert.ok(crossing.display.position);
  assert.ok(
    Math.abs(Math.abs(crossing.display.position.longitude) - 180) < 1e-6,
  );
  assert.equal(first(state, 390).display.mode, "held");
  assert.equal(first(state, 600).stale, true);
});

test("interpolates by observation timestamps on a delayed timeline, keeping metadata untouched", () => {
  const state = new AircraftState();
  state.apply(snapshot(0), 0);
  state.apply(snapshot(30), 30_000);
  const halfway = first(state, 50); // display time 15 seconds, not receipt time 50
  assert.equal(halfway.display.mode, "interpolated");
  assert.ok(
    Math.abs((halfway.display.position?.longitude ?? 0) - 0.015) < 1e-8,
  );
  assert.equal(halfway.display.position?.height, 3150);
  assert.equal(halfway.display.timestamp, epoch + 15_000);
  assert.equal(halfway.observation.longitude_deg, 0.03);
  assert.equal(halfway.observation.geometric_altitude_m, 3300);
  assert.equal(halfway.ageSeconds, 20);
});

test("retains the preceding segment across a poll boundary and never extrapolates", () => {
  const state = new AircraftState();
  state.apply(snapshot(0), 0);
  state.apply(snapshot(30), 30_000);
  const before = first(state, 60).display.position;
  state.apply(snapshot(60), 60_000);
  const after = first(state, 60).display.position;
  assert.deepEqual(after, before); // needs the older bracketing sample at this delay
  assert.equal(first(state, 100).display.mode, "held");
  assert.equal(first(state, 100).display.position?.longitude, 0.06);
  assert.equal(first(state, 149).display.position?.longitude, 0.06);
  assert.equal(first(state, 150).stale, true);
  assert.equal(first(state, 150).display.mode, "observed");
  assert.equal(state.current(360_000, smooth).length, 0);
});

test("holds one sample, handles corrections/duplicates and clears history on stream restart or removal", () => {
  const state = new AircraftState();
  state.apply(snapshot(0), 0);
  assert.equal(first(state, 10).display.mode, "observed");
  state.apply(snapshot(30), 30_000);
  assert.equal(state.apply(snapshot(30), 31_000), false);
  const corrected = snapshot(30, { longitude_deg: 0.04 });
  corrected.sequence++;
  state.apply(corrected, 31_000);
  assert.equal(first(state, 50).display.mode, "observed");
  const restarted = snapshot(60);
  restarted.stream_id = "new-stream";
  state.apply(restarted, 60_000);
  assert.equal(first(state, 70).display.mode, "observed");
  const empty = snapshot(90);
  empty.stream_id = "new-stream";
  empty.data.aircraft = [];
  state.apply(empty, 90_000);
  const reappeared = snapshot(120);
  reappeared.stream_id = "new-stream";
  state.apply(reappeared, 120_000);
  assert.equal(first(state, 130).display.mode, "observed");
});

test("rejects long gaps, jumps, stale bracketing samples and incompatible altitude availability", () => {
  for (const [second, time, expected] of [
    [snapshot(61), 76, "gap"],
    [snapshot(30, { longitude_deg: 30 }), 50, "jump"],
    [
      snapshot(30, {
        geometric_altitude_m: null,
        geometric_altitude_reference: null,
      }),
      50,
      "Altitude",
    ],
  ] as const) {
    const state = new AircraftState();
    state.apply(snapshot(0), 0);
    state.apply(second, Date.parse(second.data.emitted_at) - epoch);
    assert.equal(first(state, time).display.mode, "observed");
    assert.match(first(state, time).display.reason, new RegExp(expected, "i"));
  }
  const state = new AircraftState();
  state.apply(snapshot(0), 0);
  const second = snapshot(30);
  second.tracking.stale_after_seconds = 40;
  state.apply(second, 30_000);
  assert.match(first(state, 50).display.reason, /Earlier sample is stale/);
});

test("handles date-line crossings, stationary samples, and unknown height without inventing altitude", () => {
  const state = new AircraftState();
  const nullHeight = {
    geometric_altitude_m: null,
    geometric_altitude_reference: null,
  } as const;
  state.apply(snapshot(0, { longitude_deg: 179.9, ...nullHeight }), 0);
  state.apply(snapshot(30, { longitude_deg: -179.9, ...nullHeight }), 30_000);
  const midpoint = first(state, 50).display.position;
  assert.ok(midpoint);
  assert.ok(Math.abs(Math.abs(midpoint.longitude) - 180) < 1e-7);
  assert.equal(midpoint.height, 0);
  assert.equal(midpoint.projected, true);
  const stationary = new AircraftState();
  stationary.apply(snapshot(0), 0);
  stationary.apply(
    snapshot(30, { longitude_deg: 0, geometric_altitude_m: 3000 }),
    30_000,
  );
  const fixed = first(stationary, 50).display.position;
  assert.ok(
    fixed &&
      Number.isFinite(fixed.latitude) &&
      Number.isFinite(fixed.longitude),
  );
  assert.ok(Math.abs(fixed.longitude) < 1e-8);
});

test("surface display projects known height without altering observations; latest mode disables motion", () => {
  const state = new AircraftState();
  state.apply(snapshot(0), 0);
  state.apply(snapshot(30), 30_000);
  const projected = first(state, 50, {
    interpolate: true,
    showAltitude: false,
  });
  assert.equal(projected.display.position?.height, 0);
  assert.equal(projected.display.position?.projected, true);
  assert.equal(projected.observation.geometric_altitude_m, 3300);
  const latest = first(state, 50, { interpolate: false, showAltitude: true });
  assert.equal(latest.display.position?.longitude, 0.03);
  assert.equal(latest.display.mode, "observed");
});

test("ground-track cues honor zero/ninety degrees, omit unknown track and are not heading inference", () => {
  const position = {
    latitude: 0,
    longitude: 0,
    height: 3000,
    projected: false,
  };
  assert.equal(trackEndpoint(position, null), null);
  const north = trackEndpoint(position, 0);
  const east = trackEndpoint(position, 90);
  assert.ok(north && north.latitude > 0.03 && Math.abs(north.longitude) < 1e-8);
  assert.ok(east && east.longitude > 0.03 && Math.abs(east.latitude) < 1e-8);
  assert.equal(east.height, 3000);
});

test("filters use literal case-insensitive callsign/ICAO/type matching and freshness, including metadata only", () => {
  const state = new AircraftState();
  const sample = snapshot(0, { aircraft_type: "A320" });
  state.apply(sample, 0);
  const planes = state.current(0);
  assert.equal(
    filterAircraft(planes, { query: "sYnTh", freshOnly: false }).length,
    1,
  );
  assert.equal(
    filterAircraft(planes, { query: "000001", freshOnly: false }).length,
    1,
  );
  assert.equal(
    filterAircraft(planes, { query: " a320 ", freshOnly: true }).length,
    1,
  );
  assert.equal(
    filterAircraft(planes, { query: ".*", freshOnly: false }).length,
    0,
  );
  assert.equal(
    filterAircraft(state.current(90_000), { query: "", freshOnly: true })
      .length,
    0,
  );
  state.apply(
    snapshot(30, {
      latitude_deg: null,
      longitude_deg: null,
      position_observed_at: null,
    }),
    30_000,
  );
  const metadataOnly = state.current(30_000);
  assert.equal(metadataOnly[0]?.display.position, null);
  assert.equal(
    filterAircraft(metadataOnly, { query: "000001", freshOnly: false }).length,
    1,
  );
  assert.equal(
    filterAircraft(metadataOnly, { query: "000001", freshOnly: true }).length,
    0,
  );
});
