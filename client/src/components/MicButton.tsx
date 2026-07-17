/* Voice input (WP12, spec §7): MediaRecorder -> POST /stt -> text into the
   composer at the cursor.

   Pointer modes:
     fine pointer   hold-to-record — pointerdown starts, pointerup (anywhere)
                    stops; releases under 300ms cancel with a "hold" hint.
     coarse pointer tap-to-toggle — one tap starts, another stops.

   States: idle -> recording -> transcribing -> idle. A 501 from /stt means no
   STT engine is installed server-side: the button disables permanently for
   this page load with the server's detail as the title. Mic permission
   denials keep the button enabled (the user can grant and retry) with a hint
   in the title. The component renders nothing when MediaRecorder/getUserMedia
   are unavailable (the reserved slot simply disappears). */

import { useEffect, useRef, useState } from "react";

import { ApiError, transcribeAudio } from "../api";

const MIN_RECORD_MS = 300;

const isCoarsePointer =
  typeof window !== "undefined" &&
  window.matchMedia("(pointer: coarse)").matches;

export function micSupported(): boolean {
  return (
    typeof MediaRecorder !== "undefined" &&
    typeof navigator !== "undefined" &&
    navigator.mediaDevices?.getUserMedia !== undefined
  );
}

/** First MediaRecorder mime type this browser supports (opus-webm preferred;
    Safari records mp4/aac). Empty string = let the browser pick. */
function pickMimeType(): string {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c;
  }
  return "";
}

/** Server-friendly filename for the recorded blob's mime type. */
function filenameFor(mime: string): string {
  if (mime.includes("ogg")) return "voice.ogg";
  if (mime.includes("mp4")) return "voice.m4a";
  return "voice.webm";
}

type MicState = "idle" | "recording" | "transcribing";

export interface MicButtonProps {
  /** Disable temporarily (edit mode). */
  disabled: boolean;
  /** Transcribed text ready to insert at the cursor. */
  onText: (text: string) => void;
  /** Brief inline error (network/server failure) — composer error bar. */
  onError: (detail: string) => void;
}

export function MicButton({ disabled, onText, onError }: MicButtonProps) {
  const [state, setState] = useState<MicState>("idle");
  const [elapsedMs, setElapsedMs] = useState(0);
  /** 501 from the server: engine unavailable — disable with the detail. */
  const [unavailable, setUnavailable] = useState<string | null>(null);
  /** Transient title hint (permission denied / released too fast). */
  const [hint, setHint] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startedAtRef = useRef(0);
  /** True when the release came before MIN_RECORD_MS — discard the take. */
  const cancelledRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // Unmount mid-recording (channel switch): drop everything.
      cancelledRef.current = true;
      recorderRef.current?.stop();
      stopTracks();
      stopTimer();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stopTracks = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  };

  const stopTimer = () => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  const finishRecording = (blob: Blob, mime: string) => {
    if (!mountedRef.current) return;
    if (blob.size === 0) {
      setState("idle");
      return;
    }
    setState("transcribing");
    transcribeAudio(blob, filenameFor(mime)).then(
      ({ text }) => {
        if (!mountedRef.current) return;
        setState("idle");
        const trimmed = text.trim();
        if (trimmed.length > 0) onText(trimmed);
        else setHint("Nothing was transcribed — try speaking closer to the mic");
      },
      (err: unknown) => {
        if (!mountedRef.current) return;
        setState("idle");
        if (err instanceof ApiError && err.status === 501) {
          setUnavailable(err.detail);
        } else {
          onError(
            err instanceof ApiError ? err.detail : "Transcription failed",
          );
        }
      },
    );
  };

  const start = async () => {
    if (state !== "idle" || disabled || unavailable !== null) return;
    setHint(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      const denied =
        err instanceof DOMException &&
        (err.name === "NotAllowedError" || err.name === "SecurityError");
      setHint(
        denied
          ? "Microphone access denied — allow the mic for this site, then try again"
          : "Could not open the microphone",
      );
      return;
    }
    if (!mountedRef.current) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    streamRef.current = stream;
    const mime = pickMimeType();
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(
        stream,
        mime !== "" ? { mimeType: mime } : undefined,
      );
    } catch {
      stopTracks();
      setHint("Recording is not supported in this browser");
      return;
    }
    chunksRef.current = [];
    cancelledRef.current = false;
    recorder.ondataavailable = (e: BlobEvent) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorder.onstop = () => {
      stopTracks();
      stopTimer();
      const type = recorder.mimeType || mime || "audio/webm";
      const blob = new Blob(chunksRef.current, { type });
      chunksRef.current = [];
      recorderRef.current = null;
      if (cancelledRef.current) {
        if (mountedRef.current) setState("idle");
        return;
      }
      finishRecording(blob, type);
    };
    recorderRef.current = recorder;
    startedAtRef.current = Date.now();
    setElapsedMs(0);
    setState("recording");
    recorder.start();
    stopTimer();
    timerRef.current = setInterval(() => {
      setElapsedMs(Date.now() - startedAtRef.current);
    }, 250);
  };

  const stop = () => {
    const recorder = recorderRef.current;
    if (recorder === null || recorder.state === "inactive") return;
    if (Date.now() - startedAtRef.current < MIN_RECORD_MS) {
      cancelledRef.current = true;
      if (isCoarsePointer) {
        // A toggle tap can't be "released too early" — only hold-mode is.
        cancelledRef.current = false;
      } else {
        setHint("Hold the mic button while you speak");
      }
    }
    recorder.stop();
  };

  // Hold mode: the release can happen anywhere on the page.
  useEffect(() => {
    if (isCoarsePointer || state !== "recording") return;
    const onUp = () => stop();
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  if (!micSupported()) return null;

  const recording = state === "recording";
  const seconds = Math.floor(elapsedMs / 1000);
  const clock = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

  const title =
    unavailable ??
    hint ??
    (state === "transcribing"
      ? "Transcribing…"
      : recording
        ? isCoarsePointer
          ? "Tap to stop"
          : "Release to transcribe"
        : isCoarsePointer
          ? "Tap to record voice"
          : "Hold to record voice");

  return (
    <>
      {recording && (
        <span className="mic-elapsed" aria-live="polite">
          {clock}
        </span>
      )}
      <button
        type="button"
        className={`icon-btn composer-btn mic-btn${recording ? " recording" : ""}`}
        title={title}
        aria-label={title}
        aria-pressed={recording}
        disabled={disabled || unavailable !== null || state === "transcribing"}
        onContextMenu={(e) => e.preventDefault()}
        {...(isCoarsePointer
          ? { onClick: () => (recording ? stop() : void start()) }
          : {
              onPointerDown: (e) => {
                e.preventDefault(); // keep composer focus
                void start();
              },
            })}
      >
        {state === "transcribing" ? "…" : "🎙"}
      </button>
    </>
  );
}
