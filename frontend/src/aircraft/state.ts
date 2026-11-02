import type { Snapshot } from "../network/snapshot.ts";
import {
  displayPosition,
  interpolateMotion,
  MAX_SAMPLES,
  type Motion,
  observedMotion,
  type PositionSample,
  positionSample,
} from "./motion.ts";

export { displayPosition } from "./motion.ts";

export interface DisplayAircraft {
  observation: Snapshot["data"]["aircraft"][number];
  ageSeconds: number;
  stale: boolean;
  display: Motion;
}
export interface MotionOptions {
  interpolate: boolean;
  showAltitude: boolean;
}

export class AircraftState {
  snapshot: Snapshot | null = null;
  private receivedAt = 0;
  private readonly histories = new Map<string, PositionSample[]>();

  apply(snapshot: Snapshot, monotonicNow: number): boolean {
    if (
      this.snapshot?.stream_id === snapshot.stream_id &&
      snapshot.sequence <= this.snapshot.sequence
    )
      return false;
    if (
      this.snapshot?.stream_id !== snapshot.stream_id ||
      this.snapshot.data.source !== snapshot.data.source
    )
      this.histories.clear();
    const present = new Set(
      snapshot.data.aircraft.map((plane) => plane.icao24),
    );
    for (const id of this.histories.keys())
      if (!present.has(id)) this.histories.delete(id);
    for (const plane of snapshot.data.aircraft) {
      if (!displayPosition(plane)) {
        this.histories.delete(plane.icao24);
        continue;
      }
      let samples = this.histories.get(plane.icao24) ?? [];
      const previous = samples.at(-1);
      const time = Date.parse(plane.position_observed_at as string);
      if (
        previous &&
        time === previous.time &&
        JSON.stringify(displayPosition(previous.observation)) ===
          JSON.stringify(displayPosition(plane))
      )
        continue;
      // A correction at the same time or a regressing time must never form a
      // zero/negative-duration interpolation segment.
      if (previous && time <= previous.time) samples = [];
      const sampleGap =
        snapshot.tracking.coverage_kind === "sampled_hubs"
          ? Math.min(240, (snapshot.tracking.coverage_cycle_seconds ?? 30) + 30)
          : 60;
      samples.push(positionSample(plane, samples.at(-1), sampleGap));
      this.histories.set(plane.icao24, samples.slice(-MAX_SAMPLES));
    }
    this.snapshot = snapshot;
    this.receivedAt = monotonicNow;
    return true;
  }
  elapsedSeconds(monotonicNow: number): number {
    return Math.max(0, monotonicNow - this.receivedAt) / 1000;
  }
  get delaySeconds(): number {
    return Math.min(
      this.snapshot?.tracking.coverage_kind === "sampled_hubs" ? 210 : 60,
      (this.snapshot?.tracking.coverage_cycle_seconds ??
        this.snapshot?.tracking.poll_interval_seconds ??
        30) + 5,
    );
  }
  current(
    monotonicNow: number,
    options: MotionOptions = { interpolate: false, showAltitude: true },
  ): DisplayAircraft[] {
    const snapshot = this.snapshot;
    if (!snapshot) return [];
    // Anchor aging to server emission time; the browser's wall clock may differ.
    const now =
      Date.parse(snapshot.data.emitted_at) +
      this.elapsedSeconds(monotonicNow) * 1000;
    return snapshot.data.aircraft.flatMap((plane) => {
      const timestamp =
        plane.position_observed_at ??
        plane.last_message_at ??
        plane.provider_timestamp;
      const ageSeconds = Math.max(0, (now - Date.parse(timestamp)) / 1000);
      if (ageSeconds >= snapshot.tracking.expire_after_seconds) {
        this.histories.delete(plane.icao24);
        return [];
      }
      const stale =
        plane.position_observed_at === null ||
        ageSeconds >= snapshot.tracking.stale_after_seconds;
      const display =
        options.interpolate && !stale && plane.position_observed_at !== null
          ? interpolateMotion(
              this.histories.get(plane.icao24) ?? [],
              plane,
              now - this.delaySeconds * 1000,
              now,
              snapshot.tracking.stale_after_seconds,
              options.showAltitude,
            )
          : observedMotion(
              plane,
              options.showAltitude,
              stale
                ? "Stale observation; motion stopped"
                : "Latest observation",
            );
      return [{ observation: plane, ageSeconds, stale, display }];
    });
  }
}
