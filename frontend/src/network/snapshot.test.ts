import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "./fixture.ts";
import {
  type Aircraft,
  decodeSnapshot,
  fetchSnapshot,
  parseSnapshot,
} from "./snapshot.ts";

test("accepts the backend-produced synthetic contract without inventing missing fields", () => {
  const snapshot = fixture();
  assert.equal(snapshot.data.aircraft.length, 2);
  assert.equal(snapshot.data.aircraft[0]?.geometric_altitude_m, 3000);
  assert.equal(snapshot.data.aircraft[1]?.geometric_altitude_m, null);
  assert.equal(snapshot.data.aircraft[0]?.ground_track_deg, null);
  assert.deepEqual(decodeSnapshot(JSON.stringify(snapshot)), snapshot);
});

test("fetch uses the shared local cache with a deadline, without caching HTTP responses", async () => {
  const expected = fixture();
  assert.deepEqual(
    await fetchSnapshot(async (url, options) => {
      assert.equal(url, "/api/aircraft");
      assert.equal(options?.cache, "no-store");
      assert.ok(options?.signal instanceof AbortSignal);
      return Response.json(expected);
    }),
    expected,
  );
  await assert.rejects(
    fetchSnapshot(async () => Response.json(expected, { status: 503 })),
    /HTTP 503/,
  );
  await assert.rejects(fetchSnapshot(async () => new Response("not JSON")));
  await assert.rejects(
    fetchSnapshot(async () => {
      throw new TypeError("offline");
    }),
    /offline/,
  );
});

test("rejects incompatible envelopes, invalid limits and duplicate identifiers", () => {
  const valid = fixture();
  for (const value of [
    null,
    {},
    { ...valid, schema_version: 2 },
    { ...valid, sequence: -1 },
    { ...valid, tracking: { ...valid.tracking, stale_after_seconds: 300 } },
    { ...valid, data: { ...valid.data, source: "other" } },
    {
      ...valid,
      data: {
        ...valid.data,
        aircraft: [valid.data.aircraft[0], valid.data.aircraft[0]],
      },
    },
  ])
    assert.throws(() => parseSnapshot(value), /invalid aircraft snapshot/);
  assert.throws(
    () => decodeSnapshot(new ArrayBuffer(10)),
    /invalid aircraft snapshot/,
  );
});

test("rejects inconsistent or unsafe coordinates, heights and times", () => {
  for (const patch of [
    { latitude_deg: 91 },
    { longitude_deg: "0" },
    { latitude_deg: Number.NaN },
    { position_observed_at: null },
    { position_observed_at: "2026-10-19T12:01:00Z" },
    { received_at: "2026-10-19T12:00:00" },
    { geometric_altitude_reference: null },
    { geometric_altitude_m: Infinity },
    { groundspeed_mps: -1 },
    { ground_track_deg: 360 },
    { icao24: "<bad>" },
    { source: "adsblol" },
  ]) {
    const snapshot = fixture();
    Object.assign(snapshot.data.aircraft[0] as Aircraft, patch);
    assert.throws(() => parseSnapshot(snapshot), /invalid aircraft snapshot/);
  }
});

test("accepts metadata without position and distinguishes outage from an empty success", () => {
  const snapshot = fixture();
  Object.assign(snapshot.data.aircraft[1] as Aircraft, {
    latitude_deg: null,
    longitude_deg: null,
    position_observed_at: null,
  });
  snapshot.data.status = "degraded";
  snapshot.data.last_error = "Provider unavailable";
  assert.equal(parseSnapshot(snapshot).data.aircraft.length, 2);
  snapshot.data.status = "healthy";
  snapshot.data.last_error = null;
  snapshot.data.aircraft = [];
  assert.equal(parseSnapshot(snapshot).data.aircraft.length, 0);
});
