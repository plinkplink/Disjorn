/* Web Push subscription flow (WP11). Spec §10: the permission prompt lives in
   Settings ONLY. On page load main.tsx calls probeSubscribed(), a
   prompt-free, network-free lookup the sound gate needs, and the shell calls
   sync(), which never prompts either: it re-uploads this device's existing
   subscription (the first upload may have been lost) and, if the reader
   enabled push here and never disabled it, quietly re-subscribes under the
   permission already granted.

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

   enable() = Notification.requestPermission() -> GET /vapid-public-key ->
   pushManager.subscribe(userVisibleOnly) -> POST /push/subscribe.
   The prompt goes first because iOS only allows it inside the tap's user
   activation, which a slow first request can outlast.
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

/** How long to wait for the worker to activate before calling push dead here. */
const SW_READY_TIMEOUT_MS = 10_000;

/** Remembers "the reader turned push on in this browser" across loads. */
const WANTED_KEY = "disjorn.push.wanted";

function setWanted(wanted: boolean): void {
  try {
    if (wanted) localStorage.setItem(WANTED_KEY, "1");
    else localStorage.removeItem(WANTED_KEY);
  } catch {
    /* storage blocked: sync() just will not re-subscribe on its own */
  }
}

function wanted(): boolean {
  try {
    return localStorage.getItem(WANTED_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * The registration once its worker is ACTIVE. pushManager.subscribe rejects
 * against a worker that is still installing, which is exactly the state of a
 * first visit or a freshly cleared app.
 */
async function registration(): Promise<ServiceWorkerRegistration | undefined> {
  if ((await navigator.serviceWorker.getRegistration()) === undefined) {
    return undefined;
  }
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise<undefined>((resolve) =>
      setTimeout(() => resolve(undefined), SW_READY_TIMEOUT_MS),
    ),
  ]);
}

let vapidKey: string | null = null;

async function vapidPublicKey(): Promise<string> {
  if (vapidKey === null) vapidKey = (await getVapidPublicKey()).key;
  return vapidKey;
}

async function subscribeAndUpload(
  reg: ServiceWorkerRegistration,
  key: string,
): Promise<void> {
  const sub =
    (await reg.pushManager.getSubscription()) ??
    (await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key).buffer as ArrayBuffer,
    }));
  const json = sub.toJSON();
  if (json.endpoint === undefined) throw new Error("subscription has no endpoint");
  await pushSubscribe(json.endpoint, json.keys ?? {});
}

interface PushState {
  status: PushStatus;
  /** Human-readable context for not-configured / error states. */
  detail: string | null;
  /** True while enable()/disable() is in flight (buttons disable on it). */
  busy: boolean;
  /** This browser holds a push subscription; null until first probed. */
  subscribed: boolean | null;

  /** Passive probe — no permission prompt, ever. Settings calls it on mount. */
  refresh: () => Promise<void>;
  /** Sets `subscribed` only; never prompts, never touches the server. */
  probeSubscribed: () => Promise<void>;
  /** Boot-time repair, no prompt, never rejects. The shell calls it once. */
  sync: () => Promise<void>;
  enable: () => Promise<void>;
  disable: () => Promise<void>;
}

export const usePush = create<PushState>()((set, get) => ({
  status: "checking",
  detail: null,
  busy: false,
  subscribed: null,

  refresh: async () => {
    if (!supported()) {
      set({ status: "unsupported", detail: null });
      return;
    }
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = await reg?.pushManager.getSubscription();
      set({ subscribed: sub != null });
      if (sub != null) {
        set({ status: "enabled", detail: null });
        return;
      }
      if (Notification.permission === "denied") {
        set({ status: "blocked", detail: null });
        return;
      }
      // Probe server config so "not configured" shows before any prompt.
      await vapidPublicKey();
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

  probeSubscribed: async () => {
    if (!supported()) {
      set({ subscribed: false });
      return;
    }
    try {
      // Not registration(): this must not wait on a worker still installing.
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = await reg?.pushManager.getSubscription();
      set({ subscribed: sub != null });
    } catch {
      set({ subscribed: false });
    }
  },

  sync: async () => {
    if (!supported() || Notification.permission !== "granted") return;
    try {
      const reg = await registration();
      if (reg === undefined) return;
      const sub = await reg.pushManager.getSubscription();
      if (sub === null && !wanted()) return;
      await subscribeAndUpload(reg, await vapidPublicKey());
      // Adopts subscriptions made before the flag existed, so a later
      // browser-side drop is repaired for them too.
      setWanted(true);
      set({ status: "enabled", detail: null, subscribed: true });
    } catch {
      /* the next load tries again; Settings shows the live state */
    }
  },

  enable: async () => {
    if (get().busy) return;
    set({ busy: true });
    try {
      // Before any await: see the header on iOS user activation.
      const permission = await Notification.requestPermission();
      if (permission === "denied") {
        set({ status: "blocked", detail: null });
        return;
      }
      if (permission !== "granted") {
        set({ status: "disabled", detail: "Permission prompt dismissed" });
        return;
      }
      const key = await vapidPublicKey();
      const reg = await registration();
      if (reg === undefined) {
        set({
          status: "error",
          detail: "Service worker not ready (dev server?) — push needs the built app",
        });
        return;
      }
      setWanted(true);
      await subscribeAndUpload(reg, key);
      set({ status: "enabled", detail: null, subscribed: true });
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
    setWanted(false);
    try {
      const reg = await navigator.serviceWorker.getRegistration();
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
      set({ status: "disabled", detail: null, subscribed: false });
    } catch {
      set({ status: "error", detail: "Could not disable notifications" });
    } finally {
      set({ busy: false });
    }
  },
}));
