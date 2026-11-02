import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "./fixture.ts";
import {
  fetchHistory,
  normalizeHistoryRange,
  parseHistory,
} from "./history.ts";

function historyFixture() {
  const observation = fixture().data.aircraft[0];
  assert.ok(observation?.position_observed_at);
  const time = Date.parse(observation.position_observed_at);
  return {
    schema_version: 1,
    source: "synthetic",
    icao24: observation.icao24,
    start: new Date(time - 1000).toISOString(),
    end: new Date(time + 1000).toISOString(),
    truncated: false,
    observations: [observation],
    attribution: {
      name: "synthetic",
      attribution: "Synthetic test data",
      url: null,
      license: "MIT",
      license_url: null,
    },
  };
}

test("history ranges use explicit UTC and reject nonpositive or excessive windows", () => {
  assert.deepEqual(
    normalizeHistoryRange("2026-10-19T12:00", "2026-10-19T13:00"),
    {
      start: "2026-10-19T12:00:00.000Z",
      end: "2026-10-19T13:00:00.000Z",
    },
  );
  assert.throws(() => normalizeHistoryRange("bad", "bad"));
  assert.throws(() =>
    normalizeHistoryRange("2026-10-19T12:00", "2026-10-19T12:00"),
  );
  assert.throws(() =>
    normalizeHistoryRange("2026-10-19T12:00", "2026-10-21T12:00"),
  );
});

test("history parser validates identity, chronology, bounds and safe attributed sources", () => {
  const good = historyFixture();
  assert.equal(parseHistory(good).observations.length, 1);
  assert.equal(
    parseHistory({ ...good, observations: [], attribution: null }).observations
      .length,
    0,
  );
  assert.throws(() => parseHistory({ ...good, attribution: null }));
  assert.throws(() =>
    parseHistory({
      ...good,
      observations: [...good.observations, ...good.observations],
    }),
  );
  assert.throws(() => parseHistory({ ...good, icao24: "abcdef" }));
  assert.throws(() => parseHistory({ ...good, source: "adsblol" }));
  assert.throws(() =>
    parseHistory({ ...good, start: good.end, end: good.start }),
  );
  assert.throws(() =>
    parseHistory({
      ...good,
      attribution: { ...good.attribution, url: "javascript:alert(1)" },
    }),
  );
});

test("history requests are bounded and distinguish failures from valid empty results", async () => {
  const good = historyFixture();
  let requested = "";
  const fetcher: typeof fetch = async (url, options) => {
    requested = String(url);
    assert.equal(options?.cache, "no-store");
    assert.ok(options?.signal);
    return new Response(JSON.stringify(good));
  };
  assert.equal(
    (
      await fetchHistory(
        good.icao24,
        "synthetic",
        good.start,
        good.end,
        fetcher,
      )
    ).source,
    "synthetic",
  );
  assert.match(requested, /limit=5000/);
  const unavailable: typeof fetch = async () =>
    new Response("unavailable", { status: 503 });
  await assert.rejects(
    fetchHistory(good.icao24, "synthetic", good.start, good.end, unavailable),
    /503/,
  );
  await assert.rejects(
    fetchHistory("abcdef", "synthetic", good.start, good.end, fetcher),
    /invalid/,
  );
});
