import assert from "node:assert/strict";
import test from "node:test";
import { parseRoute, RoutePanel } from "./route.ts";

function panelRoot(): {
  root: HTMLElement;
  elements: Map<
    string,
    { textContent: string; title: string; hidden: boolean }
  >;
} {
  const elements = new Map<
    string,
    { textContent: string; title: string; hidden: boolean }
  >();
  for (const selector of [
    "#route-origin",
    "#route-destination",
    "#route-status",
    "#route-retry",
  ]) {
    elements.set(selector, { textContent: "", title: "", hidden: false });
  }
  const root = {
    querySelector(selector: string) {
      return elements.get(selector) ?? null;
    },
  } as unknown as HTMLElement;
  return { root, elements };
}

function response(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

test("accepts official snapshot airport details without treating them as current intent", () => {
  const route = parseRoute({
    status: "reference",
    callsign: "UAL123",
    lookup_callsign: "UAL123",
    airports: [
      {
        icao: "EGLL",
        iata: "LHR",
        name: "London Heathrow Airport",
        latitude: 51.4706,
        longitude: -0.461941,
      },
      {
        icao: "KEWR",
        iata: "EWR",
        name: "Newark Liberty International Airport",
        latitude: 40.692501,
        longitude: -74.168701,
      },
    ],
    retry_in_seconds: 0,
    reason: "available",
    source: "VRS standing-data snapshot",
    source_url:
      "https://github.com/vradarserver/standing-data/tree/ba4454ca8b96c0302a0e037a644d7504d9b5beb0",
    source_snapshot_commit: "ba4454ca8b96c0302a0e037a644d7504d9b5beb0",
    source_snapshot_retrieved_at: "2026-10-23T09:09:54Z",
    current_flight_confirmed: false,
  });
  assert.equal(route.airports[0]?.name, "London Heathrow Airport");
  assert.equal(route.current_flight_confirmed, false);
});

test("rejects unpaired or out-of-range airport coordinates", () => {
  const route = {
    status: "reference",
    callsign: "UAL123",
    lookup_callsign: "UAL123",
    airports: [
      { icao: "EGLL", iata: "LHR", name: "London", latitude: 51.47 },
      { icao: "KEWR", iata: "EWR", name: "Newark" },
    ],
    retry_in_seconds: 0,
    reason: "available",
    current_flight_confirmed: false,
  };
  assert.throws(() => parseRoute(route));
  assert.throws(() =>
    parseRoute({
      ...route,
      airports: [
        {
          icao: "EGLL",
          iata: "LHR",
          name: "London",
          latitude: 91,
          longitude: 0,
        },
        { icao: "KEWR", iata: "EWR", name: "Newark" },
      ],
    }),
  );
});

test("uses a fleet route reference before attempting a selected-aircraft fetch", async () => {
  const { root, elements } = panelRoot();
  const panel = new RoutePanel(root, (icao, callsign) =>
    icao === "a00001" && callsign === "UAL123"
      ? {
          status: "unknown",
          callsign,
          lookup_callsign: callsign,
          airports: [],
          retry_in_seconds: 0,
          reason: "unmatched",
          current_flight_confirmed: false,
        }
      : undefined,
  );
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("network lookup should not run");
  };
  try {
    await panel.update("a00001", "UAL123", false);
    assert.match(
      elements.get("#route-status")?.textContent ?? "",
      /No published route/i,
    );
  } finally {
    panel.destroy();
    globalThis.fetch = originalFetch;
  }
});

test("cancels a pending cooldown retry when the selected aircraft changes", async () => {
  const { root, elements } = panelRoot();
  const panel = new RoutePanel(root);
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  globalThis.fetch = async (input) => {
    requests.push(String(input));
    if (requests.length === 1) {
      return response({
        status: "waiting",
        callsign: "UAL123",
        lookup_callsign: "UAL123",
        airports: [],
        retry_in_seconds: 1,
        reason: "cooldown",
        current_flight_confirmed: false,
      });
    }
    return response({
      status: "unavailable",
      callsign: "DAL456",
      lookup_callsign: "DAL456",
      airports: [],
      retry_in_seconds: 0,
      reason: "source_error",
      current_flight_confirmed: false,
    });
  };
  try {
    await panel.update("a00001", "UAL123", false);
    await panel.update("a00002", "DAL456", false);
    await new Promise((resolve) => setTimeout(resolve, 1_100));
    assert.equal(requests.length, 2);
    assert.match(
      elements.get("#route-status")?.textContent ?? "",
      /unavailable/i,
    );
  } finally {
    panel.destroy();
    globalThis.fetch = originalFetch;
  }
});

test("does not shorten a provider Retry-After cooldown to one minute", async () => {
  const { root } = panelRoot();
  const panel = new RoutePanel(root);
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const delays: number[] = [];
  globalThis.setTimeout = ((_callback: TimerHandler, delay?: number) => {
    delays.push(Number(delay ?? 0));
    return 0 as unknown as ReturnType<typeof setTimeout>;
  }) as unknown as typeof setTimeout;
  globalThis.clearTimeout = (() => {}) as typeof clearTimeout;
  globalThis.fetch = async () =>
    response({
      status: "waiting",
      callsign: "UAL123",
      lookup_callsign: "UAL123",
      airports: [],
      retry_in_seconds: 1_800,
      reason: "cooldown",
      current_flight_confirmed: false,
    });
  try {
    await panel.update("a00001", "UAL123", false);
    assert.ok(delays.includes(1_800_000));
    assert.ok(!delays.includes(60_000));
  } finally {
    panel.destroy();
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});
