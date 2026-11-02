/** Airport observations remain separate from flight telemetry and map position. */
export class WeatherPanel {
  private controller?: AbortController;
  private last?: Weather;
  private next = 0;
  private destroyed = false;
  private readonly root: HTMLElement;
  constructor(root: HTMLElement) {
    this.root = root;
  }
  async refresh(): Promise<void> {
    if (this.controller || this.destroyed) return;
    if (this.last && Date.now() < this.next) {
      this.render(this.last);
      return;
    }
    const controller = new AbortController();
    this.controller = controller;
    const timer = setTimeout(() => controller.abort(), 12_000);
    this.text("#weather-status", "Checking the latest KDEN observation…");
    try {
      const response = await fetch("/api/weather", {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Weather service unavailable");
      const data = parseWeather(await response.json());
      if (this.destroyed) return;
      this.last = data;
      this.next = Date.now() + Math.min(3600, data.retry_in_seconds) * 1000;
      this.render(data);
    } catch {
      if (!this.destroyed)
        this.text(
          "#weather-status",
          "Weather unavailable. Check again later; aircraft tracking is independent.",
        );
    } finally {
      clearTimeout(timer);
      this.controller = undefined;
    }
  }
  private text(selector: string, value: string): void {
    const element = this.root.querySelector(selector);
    if (element) element.textContent = value;
  }
  private render(data: Weather): void {
    const observation = data.observation;
    this.text(
      "#weather-status",
      observation
        ? `${data.status !== "available" ? "Service unavailable · " : ""}${data.stale || Date.now() - Date.parse(observation.observed_at) > 5_400_000 ? "Old observation · " : "Observed "}${new Date(observation.observed_at).toLocaleString()}`
        : "No weather observation available. Check again later.",
    );
    const details = this.root.querySelector("#weather-details");
    details?.replaceChildren();
    this.text("#weather-description", observation?.description ?? "");
    if (!observation || !details) return;
    const number = (n: number | null, unit: string, scale = 1): string =>
      n === null
        ? "Unknown"
        : `${(n * scale).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${unit}`;
    const values = [
      ["Temperature", number(observation.temperature_c, "°C")],
      ["Dew point", number(observation.dewpoint_c, "°C")],
      [
        "Wind",
        `${number(observation.wind_speed_mps, "kt", 3600 / 1852)}${observation.wind_direction_deg === null ? "" : ` · from ${observation.wind_direction_deg}°`}`,
      ],
      ["Gusts", number(observation.wind_gust_mps, "kt", 3600 / 1852)],
      ["Visibility", number(observation.visibility_m, "km", 0.001)],
      [
        "Reported cloud layer",
        observation.cloud_cover === "CLR" || observation.cloud_cover === "SKC"
          ? "Clear (station report)"
          : observation.cloud_cover
            ? `${observation.cloud_cover} · ${number(observation.cloud_base_m, "ft AGL", 1 / 0.3048)}`
            : "Not reported",
      ],
    ];
    for (const [label, value] of values) {
      const term = document.createElement("dt");
      const definition = document.createElement("dd");
      term.textContent = label ?? "";
      definition.textContent = value ?? "";
      details.append(term, definition);
    }
  }
  destroy(): void {
    this.destroyed = true;
    this.controller?.abort();
  }
}

interface Observation {
  observed_at: string;
  description: string;
  temperature_c: number | null;
  dewpoint_c: number | null;
  wind_speed_mps: number | null;
  wind_gust_mps: number | null;
  wind_direction_deg: number | null;
  visibility_m: number | null;
  cloud_base_m: number | null;
  cloud_cover: string | null;
}
interface Weather {
  status: string;
  observation: Observation | null;
  stale: boolean;
  retry_in_seconds: number;
}
export function parseWeather(value: unknown): Weather {
  if (!value || typeof value !== "object") throw new Error("Invalid weather");
  const data = value as Weather;
  if (
    !["available", "unavailable", "degraded"].includes(data.status) ||
    typeof data.stale !== "boolean" ||
    !Number.isFinite(data.retry_in_seconds) ||
    data.retry_in_seconds < 0
  )
    throw new Error("Invalid weather status");
  const o = data.observation;
  if (o !== null) {
    if (
      !o ||
      !Number.isFinite(Date.parse(o.observed_at)) ||
      typeof o.description !== "string" ||
      (o.cloud_cover !== null && typeof o.cloud_cover !== "string")
    )
      throw new Error("Invalid observation");
    for (const key of [
      "temperature_c",
      "dewpoint_c",
      "wind_speed_mps",
      "wind_gust_mps",
      "wind_direction_deg",
      "visibility_m",
      "cloud_base_m",
    ] as const)
      if (
        o[key] !== null &&
        (typeof o[key] !== "number" || !Number.isFinite(o[key]))
      )
        throw new Error("Invalid weather quantity");
  }
  return data;
}
