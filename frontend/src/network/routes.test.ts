import assert from "node:assert/strict";
import test from "node:test";
import { referenceEndpoints } from "../cesium/route-reference.ts";
import { fixture } from "./fixture.ts";
import { FleetRoutes, parseFleetRoutes, routeLabel } from "./routes.ts";
import { parseSnapshot } from "./snapshot.ts";

const reference = {
  callsign: "UAL123",
  lookup_callsign: "UAL123",
  status: "reference",
  reason: "available",
  airports: [
    {
      icao: "PANC",
      iata: "ANC",
      name: "Anchorage",
      latitude: 61.17,
      longitude: -150.0,
    },
    {
      icao: "RJTT",
      iata: "HND",
      name: "Haneda",
      latitude: 35.55,
      longitude: 139.78,
    },
  ],
  current_flight_confirmed: false,
  retry_in_seconds: 0,
};
function response() {
  const snapshot = fixture();
  return {
    schema_version: 1,
    stream_id: snapshot.stream_id,
    sequence: snapshot.sequence,
    source_ready: true,
    tracked_aircraft: 1,
    confirmed_current_flights: 0,
    references: [{ icao24: "a00001", route: reference }],
  };
}

test("fleet references preserve identity and reject duplicate or confirmed flight claims", () => {
  const result = parseFleetRoutes(response());
  const route = result.references[0]?.route;
  assert.ok(route);
  const airport = route.airports[0];
  assert.ok(airport);
  assert.match(
    routeLabel(result.references[0]?.route),
    /ANC → HND · reference/,
  );
  assert.throws(() =>
    parseFleetRoutes({ ...response(), confirmed_current_flights: 1 }),
  );
  const two = {
    ...response(),
    tracked_aircraft: 2,
    references: [response().references[0], response().references[0]],
  };
  assert.throws(() => parseFleetRoutes(two));
  assert.equal(referenceEndpoints(result.references[0]?.route)?.length, 2);
  const multi = {
    ...route,
    airports: [...route.airports, airport],
  };
  assert.equal(referenceEndpoints(multi), null);
  assert.equal(
    referenceEndpoints({
      ...route,
      airports: reference.airports.map((a) => ({
        ...a,
        latitude: null,
        longitude: null,
      })),
    }),
    null,
  );
});

test("one local fleet fetch per membership, with callsign and restart isolation", async () => {
  const snapshot = fixture();
  snapshot.data.source = "adsblol";
  const aircraft = snapshot.data.aircraft[0];
  assert.ok(aircraft);
  snapshot.data.aircraft = [
    {
      ...aircraft,
      icao24: "a00001",
      callsign: "UAL123",
      source: "adsblol",
    },
  ];
  let calls = 0;
  const fleet = new FleetRoutes(() => {});
  const fetcher = (async () => {
    calls++;
    return new Response(JSON.stringify(response()));
  }) as typeof fetch;
  await fleet.update(snapshot, fetcher);
  assert.equal(fleet.get("a00001", "UAL123")?.status, "reference");
  assert.equal(fleet.get("a00001", "UAL999"), undefined);
  await fleet.update({ ...snapshot, sequence: snapshot.sequence + 1 }, fetcher);
  assert.equal(calls, 1);
  assert.match(fleet.summary(snapshot), /1\/1 endpoint references/);
  await fleet.update({ ...snapshot, stream_id: "restart" }, fetcher);
  assert.equal(fleet.get("a00001", "UAL123"), undefined);
  assert.match(fleet.summary(snapshot), /unavailable/);
  fleet.destroy();
});

test("coverage parser validates scope identity, geometry and cadence", () => {
  const snapshot = fixture();
  const extended = {
    ...snapshot,
    tracking: {
      ...snapshot.tracking,
      coverage_preset: "den",
      coverage_kind: "single_area",
      coverage_cycle_seconds: 30,
      coverage_area_count: 1,
    },
    data: {
      ...snapshot.data,
      active_scope_id: "den",
      coverage: [
        {
          id: "den",
          name: "Denver",
          area: snapshot.tracking.area,
          status: "healthy",
          last_success_at: snapshot.data.last_success_at,
          last_error: null,
          freshness_age_seconds: 0,
          stale: false,
        },
      ],
    },
  };
  assert.equal(parseSnapshot(extended).data.coverage?.[0]?.id, "den");
  assert.throws(() =>
    parseSnapshot({
      ...extended,
      tracking: { ...extended.tracking, coverage_cycle_seconds: 180 },
    }),
  );
  assert.throws(() =>
    parseSnapshot({
      ...extended,
      data: { ...extended.data, active_scope_id: "other" },
    }),
  );
});
