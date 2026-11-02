import assert from "node:assert/strict";
import test from "node:test";
import { parseRoute, RoutePanel } from "./route.ts";
import { parseWeather } from "./weather.ts";

test("weather parser preserves unknown conditions and rejects invalid numeric data", () => {
  const value = {
    status: "available",
    stale: false,
    retry_in_seconds: 600,
    observation: {
      observed_at: "2026-10-20T12:00:00Z",
      description: "Conditions not reported",
      temperature_c: 0,
      dewpoint_c: null,
      wind_speed_mps: 0,
      wind_gust_mps: null,
      wind_direction_deg: null,
      visibility_m: null,
      cloud_base_m: null,
      cloud_cover: null,
    },
  };
  assert.equal(parseWeather(value).observation?.temperature_c, 0);
  assert.throws(() =>
    parseWeather({
      ...value,
      observation: { ...value.observation, wind_speed_mps: "10" },
    }),
  );
  assert.throws(() =>
    parseWeather({
      ...value,
      observation: { ...value.observation, observed_at: "invalid" },
    }),
  );
});

test("route parser preserves a multi-leg reference without calling it a confirmed flight", () => {
  const value = {
    status: "reference",
    callsign: "UAL123",
    airports: [
      { icao: "KDEN", iata: "DEN", name: "Denver" },
      { icao: "KSFO", iata: "SFO", name: "San Francisco" },
      { icao: "KLAX", iata: "LAX", name: "Los Angeles" },
    ],
    retry_in_seconds: 0,
    current_flight_confirmed: false,
    lookup_callsign: "UAL123",
    reason: "multi_leg_reference",
  };
  assert.equal(parseRoute(value).airports.length, 3);
  assert.throws(() => parseRoute({ ...value, current_flight_confirmed: true }));
  assert.throws(() => parseRoute({ ...value, airports: [] }));
});

test("route panel retries one cooldown response and never schedules a loop", async () => {
  const elements = new Map<
    string,
    { textContent: string; title: string; hidden: boolean }
  >();
  for (const selector of [
    "#route-origin",
    "#route-destination",
    "#route-status",
    "#route-retry",
  ])
    elements.set(selector, { textContent: "", title: "", hidden: false });
  const root = {
    querySelector(selector: string) {
      return elements.get(selector) ?? null;
    },
  } as unknown as HTMLElement;
  const panel = new RoutePanel(root);
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(
      JSON.stringify(
        calls === 1
          ? {
              status: "waiting",
              callsign: "UAL123",
              lookup_callsign: "UAL123",
              airports: [],
              retry_in_seconds: 0,
              reason: "cooldown",
              current_flight_confirmed: false,
            }
          : {
              status: "unavailable",
              callsign: "UAL123",
              lookup_callsign: "UAL123",
              airports: [],
              retry_in_seconds: 0,
              reason: "source_error",
              current_flight_confirmed: false,
            },
      ),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };
  try {
    await panel.update("a00001", "UAL123", false);
    await new Promise((resolve) => setTimeout(resolve, 1_100));
    assert.equal(calls, 2);
    assert.match(
      elements.get("#route-status")?.textContent ?? "",
      /unavailable/i,
    );
    assert.equal(elements.get("#route-retry")?.hidden, false);
  } finally {
    panel.destroy();
    globalThis.fetch = originalFetch;
  }
});
