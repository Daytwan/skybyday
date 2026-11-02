export interface MapPreferences {
  imagery: "aerial" | "earth" | "offline";
  terrain: boolean;
  airport: boolean;
  places: boolean;
  landmarks: boolean;
}

export const MAP_DEFAULTS: MapPreferences = {
  imagery: "aerial",
  terrain: true,
  airport: true,
  places: true,
  landmarks: true,
};
const KEY = "skytrace.map.v1";

export function parseMapPreferences(text: string | null): MapPreferences {
  try {
    const value = JSON.parse(text ?? "{}");
    return {
      imagery:
        value?.imagery === "offline"
          ? "offline"
          : value?.imagery === "earth"
            ? "earth"
            : "aerial",
      terrain:
        typeof value?.terrain === "boolean"
          ? value.terrain
          : MAP_DEFAULTS.terrain,
      airport:
        typeof value?.airport === "boolean"
          ? value.airport
          : MAP_DEFAULTS.airport,
      places: typeof value?.places === "boolean" ? value.places : true,
      landmarks: typeof value?.landmarks === "boolean" ? value.landmarks : true,
    };
  } catch {
    return { ...MAP_DEFAULTS };
  }
}

export function readMapPreferences(): MapPreferences {
  try {
    return parseMapPreferences(localStorage.getItem(KEY));
  } catch {
    return { ...MAP_DEFAULTS };
  }
}

export function saveMapPreferences(value: MapPreferences): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    // Storage can be disabled or full; this session must remain usable.
  }
}
