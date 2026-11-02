import { type Aircraft, parseAircraft, type Source } from "./snapshot.ts";

export const MAX_HISTORY_SECONDS = 24 * 60 * 60;
export const MAX_HISTORY_OBSERVATIONS = 5000;
const MAX_HISTORY_BODY_BYTES = 32 * 1024 * 1024;

export interface SourceAttribution {
  name: Source;
  attribution: string;
  url: string | null;
  license: string;
  license_url: string | null;
}

export interface HistoryResponse {
  schema_version: 1;
  source: Source;
  icao24: string;
  start: string;
  end: string;
  truncated: boolean;
  observations: Aircraft[];
  attribution: SourceAttribution | null;
}

export interface HistoryRange {
  start: string;
  end: string;
}

function invalid(): never {
  throw new Error("The backend returned an invalid aircraft history.");
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return invalid();
  return value as Record<string, unknown>;
}

function text(value: unknown, max = 256): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max)
    return invalid();
  return value;
}

function nullableText(value: unknown, max = 4096): string | null {
  if (value === null) return null;
  return text(value, max);
}

function safeUrl(value: unknown): string | null {
  const result = nullableText(value, 2048);
  if (result === null) return null;
  const url = new URL(result);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password
  )
    return invalid();
  return result;
}

function source(value: unknown): Source {
  if (value === "synthetic" || value === "adsblol" || value === "adsbfi")
    return value;
  return invalid();
}

function utcTime(value: unknown): { value: string; milliseconds: number } {
  const result = text(value, 40);
  if (!/(Z|[+-]\d{2}:\d{2})$/.test(result)) return invalid();
  const milliseconds = Date.parse(result);
  if (!Number.isFinite(milliseconds)) return invalid();
  return { value: result, milliseconds };
}

function historyRangeMilliseconds(start: string, end: string): HistoryRange {
  const startTime = utcTime(start).milliseconds;
  const endTime = utcTime(end).milliseconds;
  if (endTime <= startTime || endTime - startTime > MAX_HISTORY_SECONDS * 1000)
    throw new Error(
      "History range must be positive and no longer than 24 hours.",
    );
  return {
    start: new Date(startTime).toISOString(),
    end: new Date(endTime).toISOString(),
  };
}

function parseInputTime(value: string): number {
  // datetime-local controls have no zone; the history form labels them UTC.
  const candidate = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const milliseconds = Date.parse(candidate);
  if (!Number.isFinite(milliseconds))
    throw new Error("History times must be valid UTC timestamps.");
  return milliseconds;
}

export function normalizeHistoryRange(
  start: string,
  end: string,
): HistoryRange {
  const startTime = parseInputTime(start);
  const endTime = parseInputTime(end);
  return historyRangeMilliseconds(
    new Date(startTime).toISOString(),
    new Date(endTime).toISOString(),
  );
}

function parseAttribution(
  value: unknown,
  expectedSource: Source,
): SourceAttribution | null {
  if (value === null) return null;
  const row = record(value);
  if (row.name !== expectedSource) return invalid();
  return {
    name: expectedSource,
    attribution: text(row.attribution, 4096),
    url: safeUrl(row.url),
    license: text(row.license, 256),
    license_url: safeUrl(row.license_url),
  };
}

export function parseHistory(value: unknown): HistoryResponse {
  const root = record(value);
  if (root.schema_version !== 1 || typeof root.truncated !== "boolean")
    return invalid();
  const historySource = source(root.source);
  const id = text(root.icao24, 6);
  if (!/^[0-9a-f]{6}$/.test(id)) return invalid();
  const start = utcTime(root.start);
  const end = utcTime(root.end);
  const range = historyRangeMilliseconds(start.value, end.value);
  if (
    !Array.isArray(root.observations) ||
    root.observations.length > MAX_HISTORY_OBSERVATIONS
  )
    return invalid();
  let previous = -Infinity;
  const observations = root.observations.map((row) => {
    const observation = parseAircraft(row, historySource);
    const time = Date.parse(observation.position_observed_at ?? "");
    if (
      observation.icao24 !== id ||
      !Number.isFinite(time) ||
      time <= previous ||
      time < start.milliseconds ||
      time >= end.milliseconds
    )
      return invalid();
    previous = time;
    return observation;
  });
  const attribution = parseAttribution(root.attribution, historySource);
  if (observations.length > 0 && attribution === null) return invalid();
  return {
    schema_version: 1,
    source: historySource,
    icao24: id,
    start: range.start,
    end: range.end,
    truncated: root.truncated,
    observations,
    attribution,
  };
}

export function decodeHistory(value: unknown): HistoryResponse {
  if (typeof value !== "string" || value.length > MAX_HISTORY_BODY_BYTES)
    return invalid();
  try {
    return parseHistory(JSON.parse(value));
  } catch (error) {
    if (
      error instanceof Error &&
      error.message.includes("invalid aircraft history")
    )
      throw error;
    throw new Error("The backend returned invalid aircraft history JSON.");
  }
}

export async function fetchHistory(
  icao24: string,
  historySource: Source,
  start: string,
  end: string,
  fetcher: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<HistoryResponse> {
  if (!/^[0-9a-f]{6}$/.test(icao24))
    throw new Error("ICAO24 must be six lowercase hexadecimal characters.");
  const range = normalizeHistoryRange(start, end);
  const query = new URLSearchParams({
    source: historySource,
    start: range.start,
    end: range.end,
    limit: String(MAX_HISTORY_OBSERVATIONS),
  });
  const timeout = AbortSignal.timeout(5000);
  const requestSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const response = await fetcher(
    `/api/history/aircraft/${icao24}?${query.toString()}`,
    { cache: "no-store", signal: requestSignal },
  );
  if (!response.ok)
    throw new Error(`History request failed: HTTP ${response.status}.`);
  const history = decodeHistory(await response.text());
  if (
    history.source !== historySource ||
    history.icao24 !== icao24 ||
    history.start !== range.start ||
    history.end !== range.end
  )
    return invalid();
  return history;
}
