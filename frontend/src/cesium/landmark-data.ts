export type Coordinate = [longitude: number, latitude: number];
export type BuildingPolygon = Coordinate[][];
export type BuildingRecord = [
  objectId: number,
  heightM: number,
  groundOrthometricM: number,
  buildingType: string,
  polygons: BuildingPolygon[],
];

export interface BuildingChunkEntry {
  count: number;
  bytes: number;
  sha256: string;
  bounds: [west: number, south: number, east: number, north: number];
}

export interface BuildingManifest {
  version: 1;
  building_count: number;
  chunk_degrees: number;
  vertical_reference: string;
  chunks: Record<string, BuildingChunkEntry>;
}

export interface ViewBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function validCoordinate(value: unknown): value is Coordinate {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    finite(value[0]) &&
    finite(value[1]) &&
    value[0] >= -180 &&
    value[0] <= 180 &&
    value[1] >= -90 &&
    value[1] <= 90
  );
}

function validRing(value: unknown): value is Coordinate[] {
  if (!Array.isArray(value) || value.length < 4) return false;
  if (!value.every(validCoordinate)) return false;
  const first = value[0];
  const last = value.at(-1);
  return first?.[0] === last?.[0] && first?.[1] === last?.[1];
}

function validRecord(value: unknown): value is BuildingRecord {
  if (!Array.isArray(value) || value.length !== 5) return false;
  const [objectId, heightM, groundM, buildingType, polygons] = value;
  return (
    Number.isSafeInteger(objectId) &&
    finite(heightM) &&
    heightM > 0 &&
    heightM <= 610 &&
    finite(groundM) &&
    groundM >= 1200 &&
    groundM <= 2200 &&
    typeof buildingType === "string" &&
    buildingType.length <= 40 &&
    Array.isArray(polygons) &&
    polygons.length > 0 &&
    polygons.every(
      (polygon) =>
        Array.isArray(polygon) &&
        polygon.length > 0 &&
        polygon.every(validRing),
    )
  );
}

export function parseBuildingChunk(value: unknown): BuildingRecord[] {
  if (
    typeof value !== "object" ||
    value === null ||
    !("version" in value) ||
    value.version !== 1 ||
    !("buildings" in value) ||
    !Array.isArray(value.buildings) ||
    value.buildings.length > 2000 ||
    !value.buildings.every(validRecord)
  ) {
    throw new Error("Invalid municipal building chunk");
  }
  return value.buildings;
}

export function parseBuildingManifest(value: unknown): BuildingManifest {
  if (
    typeof value !== "object" ||
    value === null ||
    !("version" in value) ||
    value.version !== 1 ||
    !("building_count" in value) ||
    !Number.isSafeInteger(value.building_count) ||
    !("chunk_degrees" in value) ||
    value.chunk_degrees !== 0.01 ||
    !("vertical_reference" in value) ||
    value.vertical_reference !== "NAVD88(GEOID12A) orthometric heights" ||
    !("chunks" in value) ||
    typeof value.chunks !== "object" ||
    value.chunks === null
  ) {
    throw new Error("Invalid municipal building manifest");
  }
  const chunks = value.chunks as Record<string, unknown>;
  for (const [key, entry] of Object.entries(chunks)) {
    if (
      !/^\d+-\d+$/.test(key) ||
      typeof entry !== "object" ||
      entry === null ||
      !("count" in entry) ||
      typeof entry.count !== "number" ||
      !Number.isSafeInteger(entry.count) ||
      entry.count <= 0 ||
      entry.count > 2000 ||
      !("bytes" in entry) ||
      typeof entry.bytes !== "number" ||
      !Number.isSafeInteger(entry.bytes) ||
      entry.bytes <= 0 ||
      entry.bytes > 1_000_000 ||
      !("sha256" in entry) ||
      typeof entry.sha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(entry.sha256) ||
      !("bounds" in entry) ||
      !Array.isArray(entry.bounds) ||
      entry.bounds.length !== 4 ||
      !entry.bounds.every(finite)
    ) {
      throw new Error(`Invalid municipal building manifest entry: ${key}`);
    }
  }
  return value as BuildingManifest;
}

function intersects(
  bounds: BuildingChunkEntry["bounds"],
  view: ViewBounds,
): boolean {
  return !(
    bounds[2] < view.west ||
    bounds[0] > view.east ||
    bounds[3] < view.south ||
    bounds[1] > view.north
  );
}

/** Return only the nearest visible chunks, keeping requests and GPU work bounded. */
export function visibleBuildingChunks(
  manifest: BuildingManifest,
  view: ViewBounds,
  focus: Coordinate = [
    (view.west + view.east) / 2,
    (view.south + view.north) / 2,
  ],
  limit = 16,
): string[] {
  if (view.east < view.west || view.north < view.south || limit <= 0) return [];
  const [centerLon, centerLat] = focus;
  return Object.entries(manifest.chunks)
    .filter(([, entry]) => intersects(entry.bounds, view))
    .sort(([, left], [, right]) => {
      const leftLon = (left.bounds[0] + left.bounds[2]) / 2;
      const leftLat = (left.bounds[1] + left.bounds[3]) / 2;
      const rightLon = (right.bounds[0] + right.bounds[2]) / 2;
      const rightLat = (right.bounds[1] + right.bounds[3]) / 2;
      return (
        (leftLon - centerLon) ** 2 +
        (leftLat - centerLat) ** 2 -
        ((rightLon - centerLon) ** 2 + (rightLat - centerLat) ** 2)
      );
    })
    .slice(0, limit)
    .map(([key]) => key);
}

export function representativeCoordinate(record: BuildingRecord): Coordinate {
  const ring = record[4][0]?.[0];
  if (!ring || ring.length < 2)
    throw new Error("Building has no exterior ring");
  const points = ring.slice(0, -1);
  return [
    points.reduce((sum, point) => sum + point[0], 0) / points.length,
    points.reduce((sum, point) => sum + point[1], 0) / points.length,
  ];
}
