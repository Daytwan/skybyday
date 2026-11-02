// The public AWS bucket contains Terrarium coverage through level 15.  Keep
// this ceiling finite: the 129 × 129 display grid is for visual relief, and a
// higher request level would increase visible-request cost without preserving
// the source raster's native resolution or vertical accuracy.
export const TERRAIN_MAX_LEVEL = 15;
// Keep each decoded heightmap at 16,641 float samples (about 65 KiB), well
// below the 256² source raster while preserving four times the prior mesh
// detail. The same fixed grid is used for parent and child tiles, so Cesium
// can stitch their shared edges without mixed-resolution geometry.
export const TERRAIN_SAMPLES = 129;

import {
  GEOID_GRID_COLUMNS,
  GEOID_GRID_ROWS,
  GEOID_GRID_SOUTH,
  GEOID_GRID_STEP,
  GEOID_GRID_WEST,
  GEOID12A_UNDULATION_M,
  GEOID18_UNDULATION_M,
} from "./geoid-grid.ts";

export const TERRAIN_DETAIL_MAX_LEVEL = 19;
export const TERRAIN_DETAIL_MIN_LEVEL = 15;
export const TERRAIN_DETAIL_RECTANGLE = {
  west: -105.8,
  south: 39.4,
  east: -104.4,
  north: 40.1,
} as const;

export type GeoidModel = "geoid12a" | "geoid18";

/**
 * Bilinearly interpolate the bounded NOAA NGS geoid snapshot.
 *
 * Returned N follows h = H + N. GEOID18 produces NAD83(2011) ellipsoid
 * height; GEOID12A is available only for sources explicitly tagged GEOID12A.
 * Neither result is a WGS84 frame/epoch transformation.
 */
export function geoidUndulation(
  longitudeDegrees: number,
  latitudeDegrees: number,
  model: GeoidModel = "geoid18",
): number | undefined {
  const x = (longitudeDegrees - GEOID_GRID_WEST) / GEOID_GRID_STEP;
  const y = (latitudeDegrees - GEOID_GRID_SOUTH) / GEOID_GRID_STEP;
  const epsilon = 1e-8;
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    x < -epsilon ||
    y < -epsilon ||
    x > GEOID_GRID_COLUMNS - 1 + epsilon ||
    y > GEOID_GRID_ROWS - 1 + epsilon
  )
    return undefined;
  const clampedX = Math.max(0, Math.min(GEOID_GRID_COLUMNS - 1, x));
  const clampedY = Math.max(0, Math.min(GEOID_GRID_ROWS - 1, y));
  const x0 = Math.floor(clampedX);
  const y0 = Math.floor(clampedY);
  const x1 = Math.min(GEOID_GRID_COLUMNS - 1, x0 + 1);
  const y1 = Math.min(GEOID_GRID_ROWS - 1, y0 + 1);
  const dx = clampedX - x0;
  const dy = clampedY - y0;
  const values =
    model === "geoid12a" ? GEOID12A_UNDULATION_M : GEOID18_UNDULATION_M;
  const value = (column: number, row: number): number => {
    const result = values[row * GEOID_GRID_COLUMNS + column];
    if (result === undefined || !Number.isFinite(result))
      throw new Error("Invalid bundled geoid grid");
    return result;
  };
  const south = value(x0, y0) * (1 - dx) + value(x1, y0) * dx;
  const north = value(x0, y1) * (1 - dx) + value(x1, y1) * dx;
  return south * (1 - dy) + north * dy;
}

export function navd88ToNad83Ellipsoid(
  longitudeDegrees: number,
  latitudeDegrees: number,
  orthometricHeightM: number,
  model: GeoidModel = "geoid18",
): number | undefined {
  if (!Number.isFinite(orthometricHeightM)) return undefined;
  const undulation = geoidUndulation(longitudeDegrees, latitudeDegrees, model);
  return undulation === undefined ? undefined : orthometricHeightM + undulation;
}

export interface NativeRectangle {
  west: number;
  south: number;
  east: number;
  north: number;
}

/**
 * ArcGIS export pixels sample cell centres. Expand by half a sample so a
 * response samples the fixed Cesium mesh nodes, including both tile edges.
 * Adjacent requests therefore share the exact edge coordinate.
 */
export function meshAlignedExportRectangle(
  rectangle: NativeRectangle,
  samples = TERRAIN_SAMPLES,
): NativeRectangle {
  if (
    samples < 2 ||
    rectangle.east <= rectangle.west ||
    rectangle.north <= rectangle.south
  )
    throw new Error("Invalid terrain export rectangle");
  const halfX = (rectangle.east - rectangle.west) / (2 * (samples - 1));
  const halfY = (rectangle.north - rectangle.south) / (2 * (samples - 1));
  return {
    west: rectangle.west - halfX,
    south: rectangle.south - halfY,
    east: rectangle.east + halfX,
    north: rectangle.north + halfY,
  };
}

export function intersectsTerrainDetail(
  westDegrees: number,
  southDegrees: number,
  eastDegrees: number,
  northDegrees: number,
): boolean {
  const detail = TERRAIN_DETAIL_RECTANGLE;
  return !(
    eastDegrees < detail.west ||
    westDegrees > detail.east ||
    northDegrees < detail.south ||
    southDegrees > detail.north
  );
}

/** Decode Mapzen Terrarium RGB, then resample heights (never RGB channels).
 * Source pixel centres are spread over the tile extent for display. This is
 * approximate visual relief, not an ellipsoidal survey or clearance model.
 */
export function terrariumHeights(
  rgba: Uint8ClampedArray,
  width: number,
  height: number,
  samples = TERRAIN_SAMPLES,
): Float32Array {
  if (
    width < 2 ||
    height < 2 ||
    samples < 2 ||
    rgba.length !== width * height * 4
  )
    throw new Error("Invalid terrain image dimensions");
  const at = (x: number, y: number): number => {
    const i = (y * width + x) * 4;
    if (rgba[i + 3] !== 255)
      throw new Error("Terrain contains missing samples");
    return (
      (rgba[i] ?? 0) * 256 +
      (rgba[i + 1] ?? 0) +
      (rgba[i + 2] ?? 0) / 256 -
      32768
    );
  };
  const result = new Float32Array(samples * samples);
  for (let row = 0; row < samples; row++) {
    const y = (row * (height - 1)) / (samples - 1);
    const y0 = Math.floor(y);
    const y1 = Math.min(height - 1, y0 + 1);
    for (let col = 0; col < samples; col++) {
      const x = (col * (width - 1)) / (samples - 1);
      const x0 = Math.floor(x);
      const x1 = Math.min(width - 1, x0 + 1);
      const top = at(x0, y0) * (1 - x + x0) + at(x1, y0) * (x - x0);
      const bottom = at(x0, y1) * (1 - x + x0) + at(x1, y1) * (x - x0);
      result[row * samples + col] = top * (1 - y + y0) + bottom * (y - y0);
    }
  }
  return result;
}
