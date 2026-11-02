export type Source = "synthetic" | "adsblol";
export interface CoverageRegion {
  id: string;
  name: string;
  area: { latitude_deg: number; longitude_deg: number; radius_nm: number };
  status: "idle" | "healthy" | "degraded";
  last_success_at: string | null;
  last_error: string | null;
  freshness_age_seconds: number | null;
  stale: boolean;
}
export interface Aircraft {
  icao24: string;
  source: Source;
  source_type: string;
  callsign: string | null;
  aircraft_type: string | null;
  latitude_deg: number | null;
  longitude_deg: number | null;
  geometric_altitude_m: number | null;
  geometric_altitude_reference: "wgs84_ellipsoid" | null;
  barometric_altitude_m: number | null;
  groundspeed_mps: number | null;
  ground_track_deg: number | null;
  true_heading_deg: number | null;
  on_ground: boolean | null;
  position_observed_at: string | null;
  last_message_at: string | null;
  provider_timestamp: string;
  received_at: string;
}
export interface Snapshot {
  type: "snapshot";
  schema_version: 1;
  stream_id: string;
  sequence: number;
  poller_status: "running" | "failed" | "stopped";
  tracking: {
    area: { latitude_deg: number; longitude_deg: number; radius_nm: number };
    poll_interval_seconds: number;
    stale_after_seconds: number;
    expire_after_seconds: number;
    coverage_preset?: "den" | "world_hubs_v1";
    coverage_kind?: "single_area" | "sampled_hubs";
    coverage_cycle_seconds?: number;
    coverage_area_count?: number;
  };
  data: {
    emitted_at: string;
    source: Source;
    status: "idle" | "healthy" | "degraded" | "suspended";
    last_error: string | null;
    last_success_at: string | null;
    retry_in_seconds: number | null;
    aircraft: Aircraft[];
    stale_aircraft_count: number;
    active_scope_id?: string;
    coverage?: CoverageRegion[];
    collected_aircraft_count?: number;
    published_aircraft_count?: number;
    truncated_aircraft_count?: number;
    aircraft_capacity?: number;
  };
}

function invalid(): never {
  throw new Error("The backend returned an invalid aircraft snapshot.");
}
function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return invalid();
  return value as Record<string, unknown>;
}
function number(value: unknown, min = -Infinity, max = Infinity): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < min ||
    value > max
  )
    return invalid();
  return value;
}
function integer(
  value: unknown,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
): number {
  const result = number(value, min, max);
  if (!Number.isSafeInteger(result)) return invalid();
  return result;
}
function text(value: unknown, max = 256): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max)
    return invalid();
  return value;
}
function choice<T extends string>(value: unknown, options: readonly T[]): T {
  if (typeof value !== "string" || !options.includes(value as T))
    return invalid();
  return value as T;
}
function time(value: unknown): string {
  const result = text(value, 40);
  if (
    !/(Z|[+-]\d{2}:\d{2})$/.test(result) ||
    !Number.isFinite(Date.parse(result))
  )
    return invalid();
  return result;
}
function nullable<T>(value: unknown, parse: (input: unknown) => T): T | null {
  return value === null ? null : parse(value);
}
function bearing(value: unknown): number {
  const result = number(value, 0, 360);
  if (result === 360) return invalid();
  return result;
}
export function parseAircraft(value: unknown, source: Source): Aircraft {
  const row = record(value);
  const id = text(row.icao24, 6);
  if (!/^[0-9a-f]{6}$/.test(id) || row.source !== source) return invalid();
  const latitude = nullable(row.latitude_deg, (v) => number(v, -90, 90));
  const longitude = nullable(row.longitude_deg, (v) => number(v, -180, 180));
  const positionTime = nullable(row.position_observed_at, time);
  if (
    (latitude === null) !== (longitude === null) ||
    (latitude === null) !== (positionTime === null)
  )
    return invalid();
  const providerTime = time(row.provider_timestamp);
  const messageTime = nullable(row.last_message_at, time);
  for (const timestamp of [positionTime, messageTime]) {
    if (timestamp !== null && Date.parse(timestamp) > Date.parse(providerTime))
      return invalid();
  }
  const height = nullable(row.geometric_altitude_m, number);
  const reference = nullable(row.geometric_altitude_reference, (v) =>
    choice(v, ["wgs84_ellipsoid"] as const),
  );
  if ((height === null) !== (reference === null)) return invalid();
  if (row.on_ground !== null && typeof row.on_ground !== "boolean")
    return invalid();
  return {
    icao24: id,
    source,
    source_type: text(row.source_type, 64),
    callsign: nullable(row.callsign, (v) => text(v, 32)),
    aircraft_type: nullable(row.aircraft_type, (v) => text(v, 32)),
    latitude_deg: latitude,
    longitude_deg: longitude,
    geometric_altitude_m: height,
    geometric_altitude_reference: reference,
    barometric_altitude_m: nullable(row.barometric_altitude_m, number),
    groundspeed_mps: nullable(row.groundspeed_mps, (v) => number(v, 0)),
    ground_track_deg: nullable(row.ground_track_deg, bearing),
    true_heading_deg: nullable(row.true_heading_deg, bearing),
    on_ground: row.on_ground,
    position_observed_at: positionTime,
    last_message_at: messageTime,
    provider_timestamp: providerTime,
    received_at: time(row.received_at),
  };
}

export function parseSnapshot(value: unknown): Snapshot {
  const root = record(value);
  if (root.type !== "snapshot" || root.schema_version !== 1) return invalid();
  const data = record(root.data);
  const source = choice(data.source, ["synthetic", "adsblol"]);
  const tracking = record(root.tracking);
  const area = record(tracking.area);
  const stale = number(tracking.stale_after_seconds, 1, 3600);
  const expire = number(tracking.expire_after_seconds, 1, 86400);
  if (
    stale >= expire ||
    !Array.isArray(data.aircraft) ||
    data.aircraft.length > 10_000
  )
    return invalid();
  const planes = data.aircraft.map((row) => parseAircraft(row, source));
  if (new Set(planes.map((plane) => plane.icao24)).size !== planes.length)
    return invalid();
  const coverage =
    data.coverage === undefined ? undefined : parseCoverage(data.coverage);
  let coveragePolicy = {};
  let capacity = {};
  if (data.aircraft_capacity !== undefined) {
    const published = integer(data.published_aircraft_count, 0, 10_000);
    const limit = integer(data.aircraft_capacity, 1, 10_000);
    if (published !== planes.length || published > limit) return invalid();
    capacity = {
      collected_aircraft_count: integer(
        data.collected_aircraft_count,
        0,
        60_000,
      ),
      published_aircraft_count: published,
      truncated_aircraft_count: integer(
        data.truncated_aircraft_count,
        0,
        70_000,
      ),
      aircraft_capacity: limit,
    };
  }
  if (coverage !== undefined) {
    const areaCount = integer(tracking.coverage_area_count, 1, 6);
    const kind = choice(tracking.coverage_kind, [
      "single_area",
      "sampled_hubs",
    ] as const);
    const preset = choice(tracking.coverage_preset, [
      "den",
      "world_hubs_v1",
    ] as const);
    const activeId = text(data.active_scope_id, 32);
    const cycle = number(tracking.coverage_cycle_seconds, 30, 21_600);
    if (
      areaCount !== coverage.length ||
      !coverage.some((area) => area.id === activeId) ||
      cycle !== number(tracking.poll_interval_seconds, 30, 3600) * areaCount ||
      (kind === "single_area") !== (areaCount === 1) ||
      (preset === "den") !== (areaCount === 1)
    )
      return invalid();
    coveragePolicy = {
      coverage_preset: preset,
      coverage_kind: kind,
      coverage_cycle_seconds: cycle,
      coverage_area_count: areaCount,
    };
  }
  return {
    type: "snapshot",
    schema_version: 1,
    stream_id: text(root.stream_id, 64),
    sequence: integer(root.sequence),
    poller_status: choice(root.poller_status, ["running", "failed", "stopped"]),
    tracking: {
      area: {
        latitude_deg: number(area.latitude_deg, -90, 90),
        longitude_deg: number(area.longitude_deg, -180, 180),
        radius_nm: integer(area.radius_nm, 1, 250),
      },
      poll_interval_seconds: number(tracking.poll_interval_seconds, 30, 3600),
      stale_after_seconds: stale,
      expire_after_seconds: expire,
      ...coveragePolicy,
    },
    data: {
      emitted_at: time(data.emitted_at),
      source,
      status: choice(data.status, ["idle", "healthy", "degraded", "suspended"]),
      last_error: nullable(data.last_error, (v) => text(v, 1000)),
      last_success_at: nullable(data.last_success_at, time),
      retry_in_seconds: nullable(data.retry_in_seconds, (v) => number(v, 0)),
      aircraft: planes,
      stale_aircraft_count: integer(
        data.stale_aircraft_count,
        0,
        planes.length,
      ),
      ...(coverage === undefined
        ? {}
        : { coverage, active_scope_id: text(data.active_scope_id, 32) }),
      ...capacity,
    },
  };
}

function parseCoverage(value: unknown): CoverageRegion[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 6)
    return invalid();
  const ids = new Set<string>();
  return value.map((input) => {
    const row = record(input);
    const area = record(row.area);
    const id = text(row.id, 32);
    if (
      !/^[a-z0-9_]+$/.test(id) ||
      ids.has(id) ||
      typeof row.stale !== "boolean"
    )
      return invalid();
    ids.add(id);
    return {
      id,
      name: text(row.name, 100),
      area: {
        latitude_deg: number(area.latitude_deg, -90, 90),
        longitude_deg: number(area.longitude_deg, -180, 180),
        radius_nm: integer(area.radius_nm, 1, 250),
      },
      status: choice(row.status, ["idle", "healthy", "degraded"] as const),
      last_success_at: nullable(row.last_success_at, time),
      last_error: nullable(row.last_error, (v) => text(v, 1000)),
      freshness_age_seconds: nullable(row.freshness_age_seconds, (v) =>
        number(v, 0),
      ),
      stale: row.stale,
    };
  });
}

export function decodeSnapshot(value: unknown): Snapshot {
  if (typeof value !== "string" || value.length > 16 * 1024 * 1024)
    return invalid();
  return parseSnapshot(JSON.parse(value));
}
export async function fetchSnapshot(
  fetcher: typeof fetch = fetch,
): Promise<Snapshot> {
  const response = await fetcher("/api/aircraft", {
    cache: "no-store",
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok)
    throw new Error(`Aircraft request failed: HTTP ${response.status}.`);
  return decodeSnapshot(await response.text());
}
