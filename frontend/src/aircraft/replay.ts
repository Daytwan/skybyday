import type { HistoryResponse } from "../network/history.ts";
import type { Aircraft } from "../network/snapshot.ts";
import {
  displayPosition,
  interpolateSegment,
  type Motion,
  observedMotion,
  type PositionSample,
  positionSample,
} from "./motion.ts";
import type { DisplayAircraft } from "./state.ts";

export interface ReplayRange {
  start: number;
  end: number;
}

/**
 * A bounded, one-aircraft historical timeline. The caller owns play/pause and
 * advances this clock explicitly; this class never follows the wall clock.
 */
export class HistoricalReplay {
  private history: HistoryResponse | null = null;
  private observations: Aircraft[] = [];
  private samples: PositionSample[] = [];
  private range: ReplayRange = { start: 0, end: 0 };
  private time = 0;

  load(history: HistoryResponse): void {
    this.history = history;
    this.observations = history.observations
      .slice()
      .sort((a, b) => observationTime(a) - observationTime(b));
    this.samples = [];
    for (const observation of this.observations) {
      if (!displayPosition(observation)) continue;
      const previous = this.samples.at(-1);
      const sample = positionSample(observation, previous);
      this.samples.push(sample);
    }
    this.range = {
      start: this.samples[0]?.time ?? Date.parse(history.start),
      end: this.samples.at(-1)?.time ?? Date.parse(history.end),
    };
    this.time = this.range.start;
  }

  clear(): void {
    this.history = null;
    this.observations = [];
    this.samples = [];
    this.range = { start: 0, end: 0 };
    this.time = 0;
  }

  get loaded(): boolean {
    return this.history !== null;
  }

  get response(): HistoryResponse | null {
    return this.history;
  }

  get timeline(): ReplayRange {
    return this.range;
  }

  get currentTime(): number {
    return this.time;
  }

  setTime(time: number): number {
    if (!this.history) return this.time;
    this.time = Math.min(this.range.end, Math.max(this.range.start, time));
    return this.time;
  }

  advance(milliseconds: number): number {
    return this.setTime(this.time + Math.max(0, milliseconds));
  }

  current(showAltitude: boolean): DisplayAircraft[] {
    if (!this.history || this.observations.length === 0) return [];
    const latest = latestAt(this.observations, this.time);
    if (!latest) return [];
    // Binary search avoids a fresh 5,000-element array on each animation frame.
    let low = 0;
    let high = this.samples.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if ((this.samples[middle]?.time ?? Infinity) <= this.time)
        low = middle + 1;
      else high = middle;
    }
    const previous = this.samples[low - 1];
    const next = this.samples[low];
    let display: Motion;
    if (!previous) {
      display = observedMotion(
        latest.observation,
        showAltitude,
        "No historical position at this time",
      );
    } else if (!next) {
      display = {
        ...observedMotion(
          previous.observation,
          showAltitude,
          "No later historical sample; holding last observation",
        ),
        mode: "held",
      };
    } else if (next.breakReason || !next.path) {
      display = {
        ...observedMotion(
          previous.observation,
          showAltitude,
          next.breakReason ?? "Historical segment unavailable",
        ),
        mode: "held",
      };
    } else {
      display = interpolateSegment(previous, next, this.time, showAltitude);
    }
    return [
      {
        observation: latest.observation,
        ageSeconds: Math.max(
          0,
          (this.time - observationTime(latest.observation)) / 1000,
        ),
        stale: this.time - observationTime(latest.observation) >= 60_000,
        display,
      },
    ];
  }
}

function observationTime(observation: Aircraft): number {
  return Date.parse(
    observation.position_observed_at ??
      observation.last_message_at ??
      observation.provider_timestamp,
  );
}

function latestAt(
  observations: readonly Aircraft[],
  time: number,
): { observation: Aircraft } | null {
  let result: Aircraft | null = null;
  for (const observation of observations) {
    if (observationTime(observation) > time) break;
    result = observation;
  }
  return result ? { observation: result } : null;
}
