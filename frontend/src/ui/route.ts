interface Airport {
  icao: string;
  iata: string | null;
  name: string | null;
  latitude?: number | null;
  longitude?: number | null;
}
type RouteReason =
  | "available"
  | "multi_leg_reference"
  | "unmatched"
  | "invalid_callsign"
  | "cooldown"
  | "rate_limited"
  | "source_error"
  | "malformed";
export interface Route {
  status: "reference" | "unknown" | "unavailable" | "waiting";
  airports: Airport[];
  retry_in_seconds: number;
  current_flight_confirmed: false;
  callsign: string | null;
  lookup_callsign: string | null;
  reason: RouteReason | null;
  source?: "ADSB.lol / VRS standing data" | "VRS standing-data snapshot";
  source_url?: string;
  source_snapshot_commit?: string | null;
  source_snapshot_retrieved_at?: string | null;
}

export function parseRoute(value: unknown): Route {
  if (!value || typeof value !== "object") throw new Error("Invalid route");
  const r = value as Route;
  if (
    !["reference", "unknown", "unavailable", "waiting"].includes(r.status) ||
    r.current_flight_confirmed !== false ||
    !Number.isFinite(r.retry_in_seconds) ||
    r.retry_in_seconds < 0 ||
    (r.callsign !== null && typeof r.callsign !== "string") ||
    (r.lookup_callsign !== null && typeof r.lookup_callsign !== "string") ||
    (r.reason !== null &&
      ![
        "available",
        "multi_leg_reference",
        "unmatched",
        "invalid_callsign",
        "cooldown",
        "rate_limited",
        "source_error",
        "malformed",
      ].includes(r.reason)) ||
    !Array.isArray(r.airports) ||
    r.airports.length > 12
  )
    throw new Error("Invalid route status");
  for (const a of r.airports)
    if (
      !a ||
      (a.name !== null && typeof a.name !== "string") ||
      typeof a.icao !== "string" ||
      !/^[A-Z0-9]{3,8}$/.test(a.icao) ||
      (a.iata !== null && typeof a.iata !== "string") ||
      (a.latitude !== undefined && a.latitude !== null) !==
        (a.longitude !== undefined && a.longitude !== null) ||
      (a.latitude !== undefined &&
        a.latitude !== null &&
        (!Number.isFinite(a.latitude) ||
          a.latitude < -90 ||
          a.latitude > 90)) ||
      (a.longitude !== undefined &&
        a.longitude !== null &&
        (!Number.isFinite(a.longitude) ||
          a.longitude < -180 ||
          a.longitude > 180))
    )
      throw new Error("Invalid airport");
  if (r.status === "reference" && r.airports.length < 2)
    throw new Error("Incomplete route");
  return r;
}

function airportLabel(airport: Airport): string {
  return airport.iata || airport.icao;
}

function airportTitle(airport: Airport): string {
  return airport.name || "Airport name unavailable in this source snapshot";
}

/** Never apply today's callsign reference to historical replay or a later selection. */
export class RoutePanel {
  private static readonly MAX_AUTOMATIC_RETRIES = 1;
  private key = "";
  private controller?: AbortController;
  private retryTimer?: ReturnType<typeof setTimeout>;
  private automaticRetries = 0;
  private destroyed = false;
  private readonly root: HTMLElement;
  private readonly lookup?: (
    icao: string,
    callsign: string,
  ) => Route | undefined;
  constructor(
    root: HTMLElement,
    lookup?: (icao: string, callsign: string) => Route | undefined,
  ) {
    this.root = root;
    this.lookup = lookup;
  }
  private text(selector: string, value: string, title = ""): void {
    const element = this.root.querySelector<HTMLElement>(selector);
    if (element) {
      element.textContent = value;
      element.title = title;
    }
  }
  private clearRetryTimer(): void {
    if (this.retryTimer !== undefined) {
      clearTimeout(this.retryTimer);
      this.retryTimer = undefined;
    }
  }
  private scheduleRetry(
    icao: string,
    callsign: string,
    historical: boolean,
    retryInSeconds: number,
  ): void {
    this.clearRetryTimer();
    if (
      this.destroyed ||
      historical ||
      this.automaticRetries >= RoutePanel.MAX_AUTOMATIC_RETRIES
    )
      return;
    const key = this.key;
    // Longer intervals cannot safely fit in a browser timer. Leave manual retry
    // available instead of overflowing to an immediate request.
    if (retryInSeconds > 2_147_483) return;
    const delay = Math.max(1, Math.ceil(retryInSeconds)) * 1000;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = undefined;
      if (!this.destroyed && this.key === key) {
        this.automaticRetries += 1;
        void this.update(icao, callsign, historical, true, true);
      }
    }, delay);
  }
  async update(
    icao: string | null,
    callsign: string | null,
    historical: boolean,
    retry = false,
    automatic = false,
  ): Promise<void> {
    const key = `${icao}/${callsign}/${historical}`;
    if (this.destroyed || (key === this.key && !retry)) return;
    if (key !== this.key) this.automaticRetries = 0;
    this.key = key;
    this.clearRetryTimer();
    this.controller?.abort();
    this.text("#route-origin", "Unknown");
    this.text("#route-destination", "Unknown");
    const button = this.root.querySelector<HTMLButtonElement>("#route-retry");
    if (button) {
      button.hidden = true;
      button.onclick = () =>
        void this.update(icao, callsign, historical, true, false);
    }
    if (!icao || !callsign || historical) {
      this.text(
        "#route-status",
        historical
          ? "Historical route not recorded. Live references are not applied to replay."
          : "No published route match without a callsign.",
      );
      return;
    }
    const controller = new AbortController();
    this.controller = controller;
    const deadline = setTimeout(() => controller.abort(), 12_000);
    this.text("#route-status", "Looking up published route…");
    try {
      const local = this.lookup?.(icao, callsign);
      const route = local
        ? parseRoute(local)
        : await (async () => {
            const response = await fetch(`/api/aircraft/${icao}/route`, {
              signal: controller.signal,
              cache: "no-store",
            });
            if (!response.ok) throw new Error("Route unavailable");
            return parseRoute(await response.json());
          })();
      if (this.destroyed || controller !== this.controller || key !== this.key)
        return;
      if (route.callsign !== callsign) throw new Error("Callsign changed");
      if (route.status === "reference") {
        const first = route.airports[0];
        const last = route.airports.at(-1);
        if (route.airports.length === 2 && first && last) {
          this.text("#route-origin", airportLabel(first), airportTitle(first));
          this.text(
            "#route-destination",
            airportLabel(last),
            airportTitle(last),
          );
          this.text(
            "#route-status",
            `${airportLabel(first)} → ${airportLabel(last)}. Published callsign route; this flight is unconfirmed.`,
          );
        } else {
          this.text(
            "#route-status",
            `Published route: ${route.airports.map(airportLabel).join(" → ")}. Multiple legs; current departure and destination are unconfirmed.`,
          );
        }
        const source = document.createElement("a");
        source.href =
          route.source_url || "https://github.com/vradarserver/standing-data";
        source.target = "_blank";
        source.rel = "noopener";
        source.textContent =
          route.source === "VRS standing-data snapshot"
            ? " VRS snapshot ↗"
            : " ADSB.lol / VRS ↗";
        this.root.querySelector("#route-status")?.append(source);
      } else {
        const status =
          route.status === "unknown"
            ? route.reason === "invalid_callsign"
              ? "This identifier is not a supported published airline callsign."
              : "No published route available for this callsign."
            : route.status === "waiting"
              ? automatic
                ? `Still waiting for the shared route source. ${route.retry_in_seconds} seconds remain.`
                : `Shared lookup is cooling down. Retrying once in ${route.retry_in_seconds} seconds.`
              : route.reason === "rate_limited"
                ? "The route source is rate-limited. Try again later."
                : "Route source unavailable. Aircraft telemetry is unaffected.";
        this.text("#route-status", status);
        if (route.status === "waiting") {
          this.scheduleRetry(
            icao,
            callsign,
            historical,
            route.retry_in_seconds,
          );
          if (button) button.hidden = this.retryTimer !== undefined;
        } else if (button) {
          button.hidden = route.status === "unknown";
        }
      }
    } catch {
      if (
        !this.destroyed &&
        controller === this.controller &&
        key === this.key
      ) {
        this.text(
          "#route-status",
          "Route information unavailable. No departure or destination has been inferred.",
        );
        if (button) button.hidden = false;
      }
    } finally {
      clearTimeout(deadline);
    }
  }
  destroy(): void {
    this.destroyed = true;
    this.clearRetryTimer();
    this.controller?.abort();
  }
}
