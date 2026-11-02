import { Cartographic, Math as CesiumMath, EllipsoidGeodesic } from "cesium";
import type { Aircraft } from "../network/snapshot.ts";

export const MAX_SAMPLE_GAP_SECONDS = 60;
// A conservative display guard, not an assertion of maximum aircraft speed.
const MAX_INTERPOLATION_SPEED_MPS = 1500;
export const MAX_SAMPLES = 4;

export interface Position {
  longitude: number;
  latitude: number;
  height: number;
  projected: boolean;
}
export interface Motion {
  position: Position | null;
  timestamp: number | null;
  mode: "observed" | "held" | "interpolated";
  reason: string;
}
export interface PositionSample {
  observation: Aircraft;
  time: number;
  path: EllipsoidGeodesic | null;
  breakReason: string | null;
}

export function displayPosition(
  aircraft: Aircraft,
  showAltitude = true,
): Position | null {
  if (
    aircraft.longitude_deg === null ||
    aircraft.latitude_deg === null ||
    aircraft.position_observed_at === null
  )
    return null;
  const height =
    showAltitude && aircraft.geometric_altitude_reference === "wgs84_ellipsoid"
      ? aircraft.geometric_altitude_m
      : null;
  return {
    longitude: aircraft.longitude_deg,
    latitude: aircraft.latitude_deg,
    height: height ?? 0,
    projected: height === null,
  };
}

export function positionSample(
  observation: Aircraft,
  previous?: PositionSample,
  maxGapSeconds = MAX_SAMPLE_GAP_SECONDS,
): PositionSample {
  const sample: PositionSample = {
    observation,
    time: Date.parse(observation.position_observed_at as string),
    path: null,
    breakReason: null,
  };
  const current = displayPosition(observation);
  const before = previous && displayPosition(previous.observation);
  if (!previous || !current || !before) return sample;
  const gap = (sample.time - previous.time) / 1000;
  const boundedGap = Math.min(
    240,
    Math.max(MAX_SAMPLE_GAP_SECONDS, maxGapSeconds),
  );
  if (gap <= 0 || gap > boundedGap) {
    sample.breakReason = `Observation gap exceeds ${boundedGap} seconds`;
    return sample;
  }
  // Refuse very distant/antipodal endpoints before asking Cesium's iterative
  // ellipsoid solver to construct the short regional path.
  const lat1 = CesiumMath.toRadians(before.latitude);
  const lat2 = CesiumMath.toRadians(current.latitude);
  const dlon = CesiumMath.toRadians(current.longitude - before.longitude);
  const haversine =
    Math.sin((lat2 - lat1) / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dlon / 2) ** 2;
  const approximateDistance =
    2 * 6_371_008.8 * Math.asin(Math.sqrt(Math.min(1, Math.max(0, haversine))));
  if (approximateDistance > gap * MAX_INTERPOLATION_SPEED_MPS) {
    sample.breakReason = "Position jump exceeds the display speed guard";
    return sample;
  }
  const path = new EllipsoidGeodesic(
    Cartographic.fromDegrees(before.longitude, before.latitude),
    Cartographic.fromDegrees(current.longitude, current.latitude),
  );
  if (
    !Number.isFinite(path.surfaceDistance) ||
    path.surfaceDistance / gap > MAX_INTERPOLATION_SPEED_MPS
  ) {
    sample.breakReason = "Position jump exceeds the display speed guard";
    return sample;
  }
  sample.path = path;
  return sample;
}

export function observedMotion(
  observation: Aircraft,
  showAltitude: boolean,
  reason = "Latest observation",
): Motion {
  return {
    position: displayPosition(observation, showAltitude),
    timestamp: observation.position_observed_at
      ? Date.parse(observation.position_observed_at)
      : null,
    mode: "observed",
    reason,
  };
}

export function interpolateMotion(
  samples: readonly PositionSample[],
  latest: Aircraft,
  target: number,
  now: number,
  staleSeconds: number,
  showAltitude: boolean,
): Motion {
  const first = samples[0];
  const last = samples.at(-1);
  const observed = (reason: string) =>
    observedMotion(latest, showAltitude, reason);
  if (!first || !last || samples.length < 2)
    return observed("Waiting for a second observation");
  if (target < first.time)
    return {
      ...observedMotion(first.observation, showAltitude),
      mode: "held",
      reason: "Waiting for the delayed timeline",
    };
  if (target >= last.time)
    return {
      ...observed("No later sample; holding last observation"),
      mode: "held",
    };
  for (let i = 1; i < samples.length; i++) {
    const a = samples[i - 1];
    const b = samples[i];
    if (!a || !b || target < a.time || target >= b.time) continue;
    if (b.breakReason || !b.path)
      return observed(b.breakReason ?? "No valid interpolation segment");
    if ((now - a.time) / 1000 >= staleSeconds)
      return observed("Earlier sample is stale");
    const from = displayPosition(a.observation, showAltitude);
    const to = displayPosition(b.observation, showAltitude);
    if (!from || !to) return observed("Position unavailable");
    if (from.projected !== to.projected)
      return observed("Altitude availability changed");
    const ratio = (target - a.time) / (b.time - a.time);
    const point = b.path.interpolateUsingFraction(ratio);
    return {
      position: {
        longitude:
          ((((CesiumMath.toDegrees(point.longitude) + 180) % 360) + 360) %
            360) -
          180,
        latitude: CesiumMath.toDegrees(point.latitude),
        height: from.height + (to.height - from.height) * ratio,
        projected: from.projected,
      },
      timestamp: target,
      mode: "interpolated",
      reason: "Interpolated between observations",
    };
  }
  return observed("No bracketing observations");
}

/** Interpolate one already-validated geodesic segment on a caller-owned clock. */
export function interpolateSegment(
  fromSample: PositionSample,
  toSample: PositionSample,
  target: number,
  showAltitude: boolean,
): Motion {
  const from = displayPosition(fromSample.observation, showAltitude);
  const to = displayPosition(toSample.observation, showAltitude);
  if (!from || !to || !toSample.path || toSample.breakReason)
    return observedMotion(
      fromSample.observation,
      showAltitude,
      toSample.breakReason ?? "Position unavailable",
    );
  if (from.projected !== to.projected)
    return observedMotion(
      fromSample.observation,
      showAltitude,
      "Altitude availability changed",
    );
  if (target < fromSample.time || target > toSample.time)
    return observedMotion(
      fromSample.observation,
      showAltitude,
      "Outside historical segment",
    );
  const ratio = (target - fromSample.time) / (toSample.time - fromSample.time);
  const point = toSample.path.interpolateUsingFraction(ratio);
  return {
    position: {
      longitude:
        ((((CesiumMath.toDegrees(point.longitude) + 180) % 360) + 360) % 360) -
        180,
      latitude: CesiumMath.toDegrees(point.latitude),
      height: from.height + (to.height - from.height) * ratio,
      projected: from.projected,
    },
    timestamp: target,
    mode: "interpolated",
    reason: "Interpolated between historical observations",
  };
}

// Fixed-length ground-track cue, not a speed vector or predicted destination.
export function trackEndpoint(
  position: Position,
  groundTrack: number | null,
): Position | null {
  if (groundTrack === null) return null;
  const distance = 4000 / 6_371_008.8;
  const bearing = CesiumMath.toRadians(groundTrack);
  const latitude = CesiumMath.toRadians(position.latitude);
  const longitude = CesiumMath.toRadians(position.longitude);
  const targetLatitude = Math.asin(
    Math.sin(latitude) * Math.cos(distance) +
      Math.cos(latitude) * Math.sin(distance) * Math.cos(bearing),
  );
  const targetLongitude =
    longitude +
    Math.atan2(
      Math.sin(bearing) * Math.sin(distance) * Math.cos(latitude),
      Math.cos(distance) - Math.sin(latitude) * Math.sin(targetLatitude),
    );
  return {
    ...position,
    latitude: CesiumMath.toDegrees(targetLatitude),
    longitude:
      ((((CesiumMath.toDegrees(targetLongitude) + 180) % 360) + 360) % 360) -
      180,
  };
}
