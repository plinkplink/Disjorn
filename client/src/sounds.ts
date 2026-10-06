/* In-app notification sounds: two chimes synthesized with Web Audio, the
   per-device settings that gate them, and the autoplay unlock.

   Browsers refuse to start audio before the page has seen a user gesture, so
   the AudioContext is created and resumed inside one (installSoundUnlock) and
   every play before that is silently dropped. */

import { create } from "zustand";

import type { SoundKind } from "./lib/messageSound";

export interface SoundSettings {
  enabled: boolean;
  mentionsOnly: boolean;
  /** 0..1, applied on a squared curve so the slider feels even. */
  volume: number;
}

const STORAGE_KEY = "disjorn.sounds";
const DEFAULTS: SoundSettings = { enabled: true, mentionsOnly: false, volume: 0.6 };
const THROTTLE_MS = 1500;

function load(): SoundSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULTS;
    const p = JSON.parse(raw) as Partial<SoundSettings>;
    return {
      enabled: typeof p.enabled === "boolean" ? p.enabled : DEFAULTS.enabled,
      mentionsOnly:
        typeof p.mentionsOnly === "boolean" ? p.mentionsOnly : DEFAULTS.mentionsOnly,
      volume:
        typeof p.volume === "number" && p.volume >= 0 && p.volume <= 1
          ? p.volume
          : DEFAULTS.volume,
    };
  } catch {
    return DEFAULTS;
  }
}

interface SoundSettingsState extends SoundSettings {
  update: (patch: Partial<SoundSettings>) => void;
}

export const useSoundSettings = create<SoundSettingsState>()((set, get) => ({
  ...load(),
  update: (patch) => {
    set(patch);
    const { enabled, mentionsOnly, volume } = get();
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ enabled, mentionsOnly, volume }),
      );
    } catch {
      // Private mode or full storage: the choice lasts until reload.
    }
  },
}));

/* ---- engine ---- */

interface Note {
  freq: number;
  at: number;
  peak: number;
  decay: number;
}

/* Soft: a low two-note lift. Bright: a quick rising major triad. */
const CHIMES: Record<SoundKind, Note[]> = {
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

let ctx: AudioContext | null = null;
let lastPlayedAt = -Infinity;

type AudioContextCtor = new () => AudioContext;

function contextCtor(): AudioContextCtor | undefined {
  const w = window as Window & { webkitAudioContext?: AudioContextCtor };
  return window.AudioContext ?? w.webkitAudioContext;
}

function unlock(): void {
  try {
    if (ctx === null) {
      const Ctor = contextCtor();
      if (Ctor === undefined) return;
      ctx = new Ctor();
    }
    if (ctx.state !== "running") void ctx.resume().catch(() => {});
  } catch {
    ctx = null;
  }
}

/** Listen for gestures for the life of the page: mobile browsers suspend the
    context again after the app is backgrounded. */
export function installSoundUnlock(): void {
  for (const type of ["pointerdown", "keydown", "touchend"]) {
    window.addEventListener(type, unlock, { capture: true, passive: true });
  }
}

function synth(kind: SoundKind, volume: number): void {
  if (ctx === null || ctx.state !== "running") return;
  const master = ctx.createGain();
  master.gain.value = volume * volume;
  master.connect(ctx.destination);
  const t0 = ctx.currentTime + 0.01;
  for (const note of CHIMES[kind]) {
    const start = t0 + note.at;
    const end = start + note.decay;
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.value = note.freq;
    const env = ctx.createGain();
    env.gain.setValueAtTime(0.0001, start);
    env.gain.exponentialRampToValueAtTime(note.peak, start + 0.012);
    env.gain.exponentialRampToValueAtTime(0.0001, end);
    osc.connect(env);
    env.connect(master);
    osc.start(start);
    osc.stop(end + 0.02);
  }
}

/** Play a chime if sounds are on, throttled; never throws. */
export function playSound(kind: SoundKind): void {
  const { enabled, volume } = useSoundSettings.getState();
  if (!enabled || volume <= 0) return;
  const now = performance.now();
  if (now - lastPlayedAt < THROTTLE_MS) return;
  lastPlayedAt = now;
  try {
    synth(kind, volume);
  } catch {
    // Audio is decoration; a failure here must not reach message handling.
  }
}

/** The settings page's test buttons: unthrottled, at the slider's volume. */
export function previewSound(kind: SoundKind, volume: number): void {
  unlock();
  const play = () => {
    try {
      synth(kind, volume);
    } catch {
      // As above.
    }
  };
  if (ctx !== null && ctx.state !== "running") {
    void ctx.resume().then(play, () => {});
  } else {
    play();
  }
}
