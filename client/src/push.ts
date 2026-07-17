/* Web Push subscription flow (WP11). Spec §10: the permission prompt lives in
   Settings ONLY — nothing here runs on page load except the passive state
   probe that Settings triggers when it mounts.

   State machine (status field):

     checking ──────► unsupported            (no SW / PushManager / Notification)
        │
        ├───────────► enabled                (an active subscription exists here)
        ├───────────► blocked                (Notification.permission === "denied")
        ├───────────► not-configured         (GET /vapid-public-key -> 503)
        ├───────────► disabled               (can enable)
        └───────────► error                  (probe/server failure; detail set)

     disabled ─enable()─► enabled | blocked | not-configured | error
     enabled ─disable()─► disabled | error

   enable() = GET /vapid-public-key -> Notification.requestPermission() ->
   pushManager.subscribe(userVisibleOnly) -> POST /push/subscribe.
   disable() = DELETE /push/subscribe -> subscription.unsubscribe(). */

import { create } from "zustand";

import { ApiError, getVapidPublicKey, pushSubscribe, pushUnsubscribe } from "./api";

export type PushStatus =
  | "checking"
  | "unsupported"
  | "not-configured"
  | "blocked"
  | "enabled"
  | "disabled"
  | "error";

/** VAPID key (base64url) -> the Uint8Array pushManager.subscribe wants. */
function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

function supported(): boolean {
  return (
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

async function registration(): Promise<ServiceWorkerRegistration | undefined> {
  return navigator.serviceWorker.getRegistration();
}

interface PushState {
  status: PushStatus;
  /** Human-readable context for not-configured / error states. */
  detail: string | null;
  /** True while enable()/disable() is in flight (buttons disable on it). */
  busy: boolean;

  /** Passive probe — no permission prompt, ever. Settings calls it on mount. */
  refresh: () => Promise<void>;
  enable: () => Promise<void>;
  disable: () => Promise<void>;
}

export const usePush = create<PushState>()((set, get) => ({
  status: "checking",
  detail: null,
  busy: false,

  refresh: async () => {
    if (!supported()) {
      set({ status: "unsupported", detail: null });
      return;
    }
    try {
      const reg = await registration();
      const sub = await reg?.pushManager.getSubscription();
      if (sub != null) {
        set({ status: "enabled", detail: null });
        return;
      }
      if (Notification.permission === "denied") {
        set({ status: "blocked", detail: null });
        return;
      }
      // Probe server config so "not configured" shows before any prompt.
      await getVapidPublicKey();
      set({ status: "disabled", detail: null });
    } catch (err) {
      if (err instanceof ApiError && err.status === 503) {
        set({ status: "not-configured", detail: err.detail });
      } else {
        set({
          status: "error",
          detail: err instanceof ApiError ? err.detail : "Push state check failed",
        });
      }
    }
  },

  enable: async () => {
    if (get().busy) return;
    set({ busy: true });
    try {
      const { key } = await getVapidPublicKey();
      const permission = await Notification.requestPermission();
      if (permission === "denied") {
        set({ status: "blocked", detail: null });
        return;
      }
      if (permission !== "granted") {
        set({ status: "disabled", detail: "Permission prompt dismissed" });
        return;
      }
      const reg = await registration();
      if (reg === undefined) {
        set({
          status: "error",
          detail: "Service worker not registered (dev server?) — push needs the built app",
        });
        return;
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key).buffer as ArrayBuffer,
      });
      const json = sub.toJSON();
      if (json.endpoint === undefined) throw new Error("subscription has no endpoint");
      await pushSubscribe(json.endpoint, json.keys ?? {});
      set({ status: "enabled", detail: null });
    } catch (err) {
      if (err instanceof ApiError && err.status === 503) {
        set({ status: "not-configured", detail: err.detail });
      } else {
        set({
          status: "error",
          detail:
            err instanceof ApiError
              ? err.detail
              : "Could not enable notifications on this device",
        });
      }
    } finally {
      set({ busy: false });
    }
  },

  disable: async () => {
    if (get().busy) return;
    set({ busy: true });
    try {
      const reg = await registration();
      const sub = await reg?.pushManager.getSubscription();
      if (sub != null) {
        // Server row first (needs the endpoint), then the browser side.
        try {
          await pushUnsubscribe(sub.endpoint);
        } catch {
          /* dead server rows get pruned on next failed push — still unsubscribe */
        }
        await sub.unsubscribe();
      }
      set({ status: "disabled", detail: null });
    } catch {
      set({ status: "error", detail: "Could not disable notifications" });
    } finally {
      set({ busy: false });
    }
  },
}));
