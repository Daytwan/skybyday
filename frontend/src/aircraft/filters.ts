import type { DisplayAircraft } from "./state.ts";

export interface AircraftFilter {
  query: string;
  freshOnly: boolean;
}
export function filterAircraft(
  aircraft: DisplayAircraft[],
  filter: AircraftFilter,
): DisplayAircraft[] {
  const query = filter.query.trim().toLocaleLowerCase("en-US");
  return aircraft.filter(
    ({ observation, stale }) =>
      (!filter.freshOnly || !stale) &&
      (!query ||
        [
          observation.icao24,
          observation.callsign,
          observation.aircraft_type,
        ].some((value) => value?.toLocaleLowerCase("en-US").includes(query))),
  );
}
