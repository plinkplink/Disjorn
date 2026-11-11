/* The chimes' notes and output level. Pure: sounds.ts builds the audio graph
   from these, and the level can be bounded without an AudioContext. */

import type { SoundKind } from "./messageSound";

export interface Note {
  freq: number;
  at: number;
  peak: number;
  decay: number;
}

/* Soft: a low two-note lift. Bright: a quick rising major triad. */
export const CHIMES: Record<SoundKind, Note[]> = {
  message: [
    { freq: 587.33, at: 0, peak: 0.22, decay: 0.32 },
    { freq: 783.99, at: 0.075, peak: 0.18, decay: 0.42 },
  ],
  mention: [
    { freq: 1046.5, at: 0, peak: 0.24, decay: 0.3 },
    { freq: 1318.51, at: 0.08, peak: 0.22, decay: 0.32 },
    { freq: 1567.98, at: 0.16, peak: 0.24, decay: 0.55 },
  ],
};

/** Seconds a note takes to ramp from ENVELOPE_FLOOR to its peak. */
export const ATTACK_S = 0.012;
/** Exponential ramps cannot reach zero, so envelopes start and end here. */
export const ENVELOPE_FLOOR = 0.0001;

/** Nothing limits the output, so a chime's summed envelopes times this must stay under 1. */
export const OUTPUT_GAIN = 1.5;

/** Squared so the 0..1 slider feels even. */
export function masterGain(volume: number): number {
  return volume * volume * OUTPUT_GAIN;
}
