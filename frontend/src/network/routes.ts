import { parseRoute, type Route } from "../ui/route.ts";
import type { Snapshot } from "./snapshot.ts";

export interface FleetReference {
  icao24: string;
  route: Route;
}
export interface FleetRouteResponse {
  stream_id: string;
  sequence: number;
  source_ready: boolean;
  references: FleetReference[];
}

export function parseFleetRoutes(value: unknown): FleetRouteResponse {
  if (!value || typeof value !== "object") throw new Error("Invalid routes");
  const row = value as Record<string, unknown>;
  if (
    row.schema_version !== 1 ||
    typeof row.stream_id !== "string" ||
    !row.stream_id ||
    !Number.isSafeInteger(row.sequence) ||
    (row.sequence as number) < 0 ||
    typeof row.source_ready !== "boolean" ||
    row.confirmed_current_flights !== 0 ||
    !Array.isArray(row.references) ||
    row.references.length > 10_000 ||
    row.tracked_aircraft !== row.references.length
  )
    throw new Error("Invalid fleet route status");
  const seen = new Set<string>();
  const references = row.references.map((item: unknown) => {
    if (!item || typeof item !== "object") throw new Error("Invalid reference");
    const record = item as Record<string, unknown>;
    if (
      typeof record.icao24 !== "string" ||
      !/^[0-9a-f]{6}$/.test(record.icao24) ||
      seen.has(record.icao24)
    )
      throw new Error("Invalid reference identity");
    seen.add(record.icao24);
    return { icao24: record.icao24, route: parseRoute(record.route) };
  });
  return {
    stream_id: row.stream_id,
    sequence: row.sequence as number,
    source_ready: row.source_ready,
    references,
  };
}

export function routeLabel(route: Route | undefined): string {
  if (!route) return "Route reference loading";
  if (route.status !== "reference")
    return route.status === "unavailable"
      ? "Route source unavailable"
      : "Endpoints unknown";
  if (route.airports.length !== 2) return "Multiple legs · current leg unknown";
  return `${route.airports[0]?.iata || route.airports[0]?.icao} → ${route.airports[1]?.iata || route.airports[1]?.icao} · reference`;
}

/** One local fleet request per changed membership/callsign set, never per frame. */
export class FleetRoutes {
  private key = "";
  private stream = "";
  private routes = new Map<string, Route>();
  private controller?: AbortController;
  private retryAfter = 0;
  private failed = false;
  private sourceReady = false;
  private disposed = false;
  private readonly changed: () => void;
  constructor(changed: () => void) {
    this.changed = changed;
  }

  get(icao: string, callsign: string | null): Route | undefined {
    const route = this.routes.get(icao);
    return route?.callsign === callsign ? route : undefined;
  }

  summary(snapshot: Snapshot): string {
    if (snapshot.data.source === "synthetic")
      return "Synthetic observations · current route references are not applied.";
    if (this.failed)
      return "Fleet route references unavailable. Aircraft data continues.";
    let endpoints = 0;
    let multileg = 0;
    let unknown = 0;
    let pending = 0;
    for (const plane of snapshot.data.aircraft) {
      const route = this.get(plane.icao24, plane.callsign);
      if (!route) pending++;
      else if (route.status !== "reference") unknown++;
      else if (route.airports.length === 2) endpoints++;
      else multileg++;
    }
    if (!this.sourceReady && !pending)
      return "Local route-reference source unavailable.";
    return `${endpoints}/${snapshot.data.aircraft.length} endpoint references · ${multileg} multileg · ${unknown} unknown${pending ? ` · ${pending} loading` : ""}. Current flights unconfirmed.`;
  }

  async update(
    snapshot: Snapshot,
    fetcher: typeof fetch = fetch,
  ): Promise<void> {
    if (this.disposed) return;
    if (snapshot.stream_id !== this.stream) {
      this.routes.clear();
      this.stream = snapshot.stream_id;
    }
    const key = `${snapshot.stream_id}/${snapshot.data.source}/${snapshot.data.aircraft.map((p) => `${p.icao24}:${p.callsign}`).join(";")}`;
    if (key === this.key && (!this.failed || Date.now() < this.retryAfter))
      return;
    this.key = key;
    this.controller?.abort();
    if (snapshot.data.source === "synthetic") {
      this.routes.clear();
      this.changed();
      return;
    }
    const controller = new AbortController();
    this.controller = controller;
    const deadline = setTimeout(() => controller.abort(), 10_000);
    try {
      const response = await fetcher("/api/routes", {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("Fleet routes unavailable");
      const text = await response.text();
      if (text.length > 16 * 1024 * 1024)
        throw new Error("Fleet routes too large");
      const result = parseFleetRoutes(JSON.parse(text));
      if (this.disposed || this.controller !== controller) return;
      if (result.stream_id !== snapshot.stream_id)
        throw new Error("Feed restarted");
      this.routes = new Map(
        result.references.map((item) => [item.icao24, item.route]),
      );
      this.sourceReady = result.source_ready;
      this.failed = false;
      this.changed();
    } catch {
      if (!this.disposed && this.controller === controller) {
        this.failed = true;
        this.retryAfter = Date.now() + 30_000;
        this.changed();
      }
    } finally {
      clearTimeout(deadline);
    }
  }

  destroy(): void {
    this.disposed = true;
    this.controller?.abort();
    this.routes.clear();
  }
}
