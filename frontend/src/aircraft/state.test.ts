import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "../network/fixture.ts";
import { AircraftState, displayPosition } from "./state.ts";

test("uses only ellipsoidal geometric height and marks all surface projections", () => {
  const [geometric, unknown] = fixture().data.aircraft;
  assert.ok(geometric && unknown);
  assert.equal(displayPosition(geometric)?.height, 3000);
  assert.equal(displayPosition(geometric)?.projected, false);
  assert.equal(
    displayPosition({ ...unknown, barometric_altitude_m: 9000 })?.height,
    0,
  );
  assert.equal(displayPosition(unknown)?.projected, true);
  assert.equal(
    displayPosition({
      ...unknown,
      latitude_deg: null,
      longitude_deg: null,
      position_observed_at: null,
    }),
    null,
  );
});

test("keeps observations fixed between snapshots, stales and expires while disconnected", () => {
  const state = new AircraftState();
  const snapshot = fixture();
  state.apply(snapshot, 500);
  const initial = state.current(500);
  assert.equal(initial[0]?.stale, false);
  assert.equal(state.current(90_500)[0]?.stale, true);
  assert.equal(
    state.current(90_500)[0]?.observation.longitude_deg,
    initial[0]?.observation.longitude_deg,
  );
  assert.equal(state.current(299_500).length, 2);
  assert.equal(state.current(300_500).length, 0);
});

test("uses server timestamps and supplied retention policy, independent of browser wall clock", () => {
  const state = new AircraftState();
  const snapshot = fixture();
  snapshot.tracking.stale_after_seconds = 5;
  snapshot.tracking.expire_after_seconds = 12;
  state.apply(snapshot, 9_000_000);
  assert.equal(state.current(9_004_999)[0]?.stale, false);
  assert.equal(state.current(9_005_000)[0]?.stale, true);
  assert.equal(state.current(9_012_000).length, 0);
});

test("ignores older and duplicate snapshots, accepts new streams and authoritative empty snapshots", () => {
  const state = new AircraftState();
  const snapshot = fixture();
  snapshot.sequence = 10;
  assert.equal(state.apply(snapshot, 0), true);
  assert.equal(state.apply({ ...snapshot, sequence: 9 }, 10_000), false);
  assert.equal(state.apply(snapshot, 20_000), false);
  assert.equal(state.current(90_000)[0]?.stale, true);
  const restarted = fixture();
  restarted.stream_id = "new-server-lifespan";
  assert.equal(state.apply(restarted, 90_000), true);
  assert.equal(state.current(90_000)[0]?.stale, false);
  assert.equal(
    state.apply(
      { ...restarted, sequence: 2, data: { ...restarted.data, aircraft: [] } },
      90_001,
    ),
    true,
  );
  assert.equal(state.current(90_001).length, 0);
});

test("a newer observation changes position and unknown-position metadata remains unplotted", () => {
  const state = new AircraftState();
  const first = fixture();
  state.apply(first, 0);
  const second = fixture();
  second.sequence += 1;
  const aircraft = second.data.aircraft[0];
  const unknown = second.data.aircraft[1];
  assert.ok(aircraft && unknown);
  aircraft.longitude_deg = -104.5;
  Object.assign(unknown, {
    latitude_deg: null,
    longitude_deg: null,
    position_observed_at: null,
  });
  state.apply(second, 30_000);
  assert.equal(state.current(30_000)[0]?.observation.longitude_deg, -104.5);
  assert.equal(state.current(30_000)[1]?.stale, true);
  assert.equal(displayPosition(unknown), null);
});
