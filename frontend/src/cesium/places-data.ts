export type Place = [
  id: number,
  name: string,
  lon: number,
  lat: number,
  population: number,
  capital: boolean,
];
export interface Bounds {
  west: number;
  east: number;
  south: number;
  north: number;
}

/** Geographic chunks crossing the antimeridian are handled without fetching the world. */
export function placeChunks(bounds: Bounds): string[] {
  const width = (bounds.east - bounds.west + 360) % 360;
  if (width > 30 || bounds.north - bounds.south > 30) return [];
  const keys = new Set<string>();
  const start = Math.floor((bounds.west + 180) / 10);
  const end = Math.floor((bounds.west + width + 180) / 10);
  for (
    let y = Math.max(0, Math.floor((bounds.south + 90) / 10));
    y <= Math.min(17, Math.floor((bounds.north + 90) / 10));
    y++
  )
    for (let x = start; x <= end; x++) keys.add(`${y}-${((x % 36) + 36) % 36}`);
  return [...keys].slice(0, 16);
}

export function inBounds(place: Place, bounds: Bounds): boolean {
  return (
    place[3] >= bounds.south &&
    place[3] <= bounds.north &&
    (bounds.east >= bounds.west
      ? place[2] >= bounds.west && place[2] <= bounds.east
      : place[2] >= bounds.west || place[2] <= bounds.east)
  );
}

export function parsePlaces(value: unknown): Place[] {
  if (!Array.isArray(value) || value.length > 40_000)
    throw new Error("Invalid place data");
  return value.filter(
    (p): p is Place =>
      Array.isArray(p) &&
      p.length === 6 &&
      Number.isSafeInteger(p[0]) &&
      typeof p[1] === "string" &&
      p[1].length <= 200 &&
      Number.isFinite(p[2]) &&
      p[2] >= -180 &&
      p[2] <= 180 &&
      Number.isFinite(p[3]) &&
      p[3] >= -90 &&
      p[3] <= 90 &&
      Number.isFinite(p[4]) &&
      p[4] >= 0 &&
      typeof p[5] === "boolean",
  );
}
