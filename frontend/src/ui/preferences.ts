export interface DisplayPreferences {
  units: "aviation" | "metric";
  labels: "selected" | "all" | "none";
  interpolate: boolean;
  altitude: boolean;
  tracks: boolean;
}
export const DISPLAY_DEFAULTS: DisplayPreferences = {
  units: "aviation",
  labels: "selected",
  interpolate: true,
  altitude: true,
  tracks: false,
};
const KEY = "skytrace.display.v1";
export function parseDisplayPreferences(
  raw: string | null,
): DisplayPreferences {
  const result = { ...DISPLAY_DEFAULTS };
  try {
    const input: unknown = JSON.parse(raw ?? "null");
    if (!input || typeof input !== "object") return result;
    const value = input as Record<string, unknown>;
    if (value.units === "metric" || value.units === "aviation")
      result.units = value.units;
    if (["selected", "all", "none"].includes(String(value.labels)))
      result.labels = value.labels as DisplayPreferences["labels"];
    for (const name of ["interpolate", "altitude", "tracks"] as const)
      if (typeof value[name] === "boolean") result[name] = value[name];
  } catch {
    /* Old or corrupt preferences must not prevent startup. */
  }
  return result;
}
export function readDisplayPreferences(): DisplayPreferences {
  try {
    return parseDisplayPreferences(localStorage.getItem(KEY));
  } catch {
    return { ...DISPLAY_DEFAULTS };
  }
}
export function saveDisplayPreferences(value: DisplayPreferences): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    /* Storage may be disabled; controls still work for this visit. */
  }
}
export function heightLabel(
  metres: number | null,
  units: DisplayPreferences["units"],
): string {
  if (metres === null) return "Unknown";
  const value = units === "aviation" ? metres / 0.3048 : metres;
  return `${Math.round(value).toLocaleString("en-US")} ${units === "aviation" ? "ft" : "m"}`;
}
export function speedLabel(
  metresPerSecond: number | null,
  units: DisplayPreferences["units"],
): string {
  if (metresPerSecond === null) return "Unknown";
  return units === "aviation"
    ? `${Math.round(metresPerSecond / (1852 / 3600))} kt`
    : `${metresPerSecond.toFixed(1)} m/s`;
}
