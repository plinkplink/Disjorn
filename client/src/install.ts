/* PWA install hint (WP12). Chrome/Android fires `beforeinstallprompt` early
   in the page's life — the listener must be registered at module load (this
   file is imported by main.tsx) so the event is captured long before the
   user opens Settings. Settings then shows:

     standalone display-mode          nothing (already installed)
     captured prompt (Android/desktop Chrome)  "Install app" button
     otherwise                        nothing here; the iOS Add-to-Home-Screen
                                      note renders under the push
                                      `unsupported` state (WP11's note). */

import { create } from "zustand";

/** Non-standard Chrome event — not in lib.dom. */
interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export function isStandalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // iOS Safari's non-standard flag for home-screen launches.
    (navigator as { standalone?: boolean }).standalone === true
  );
}

/** Rough iOS detection for the Add-to-Home-Screen note (incl. iPadOS 13+,
    which masquerades as macOS but is touch). */
export function isIos(): boolean {
  const ua = navigator.userAgent;
  return (
    /iPhone|iPad|iPod/.test(ua) ||
    (ua.includes("Mac") && navigator.maxTouchPoints > 1)
  );
}

interface InstallState {
  /** True while a captured beforeinstallprompt is available to fire. */
  canPrompt: boolean;
  /** True right after the user accepted the install (or appinstalled fired). */
  installed: boolean;
  promptInstall: () => Promise<void>;
}

let deferredPrompt: BeforeInstallPromptEvent | null = null;

export const useInstall = create<InstallState>()((set) => {
  if (typeof window !== "undefined") {
    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault(); // no mini-infobar; we prompt from Settings
      deferredPrompt = e as BeforeInstallPromptEvent;
      set({ canPrompt: true });
    });
    window.addEventListener("appinstalled", () => {
      deferredPrompt = null;
      set({ canPrompt: false, installed: true });
    });
  }

  return {
    canPrompt: false,
    installed: false,

    promptInstall: async () => {
      const prompt = deferredPrompt;
      if (prompt === null) return;
      deferredPrompt = null;
      set({ canPrompt: false });
      await prompt.prompt();
      const choice = await prompt.userChoice;
      if (choice.outcome === "accepted") set({ installed: true });
      // Dismissed: the browser may fire beforeinstallprompt again later —
      // the listener above re-arms canPrompt if it does.
    },
  };
});
