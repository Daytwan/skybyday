import type { Route } from "../ui/route.ts";

export interface ReferenceEndpoint {
  code: string;
  latitude: number;
  longitude: number;
}

/** Airport reference connections never choose a leg or pass through the aircraft. */
export function referenceEndpoints(route?: Route): ReferenceEndpoint[] | null {
  if (route?.status !== "reference" || route.airports.length !== 2) return null;
  const endpoints: ReferenceEndpoint[] = [];
  for (const airport of route.airports) {
    const { latitude, longitude } = airport;
    if (
      typeof latitude !== "number" ||
      !Number.isFinite(latitude) ||
      typeof longitude !== "number" ||
      !Number.isFinite(longitude) ||
      Math.abs(latitude) > 90 ||
      Math.abs(longitude) > 180
    )
      return null;
    endpoints.push({ code: airport.iata || airport.icao, latitude, longitude });
  }
  return endpoints;
}
