/* Service-worker registration and update plumbing.

   Registered at module load rather than inside a component, so it starts
   before React mounts and isn't re-run by StrictMode's double-effect in dev.

   WHY A PROMPT AND NOT AN AUTO-RELOAD
   The worker used to skipWaiting() on install and reload the page itself.
   For a chat app that means the page can vanish mid-message. Now the new
   worker installs and waits, onNeedRefresh fires, and the reader decides.

   WHY THE POLL
   A browser only checks for a newer service worker on navigation, or roughly
   once a day. A tab left open therefore keeps running whatever build it
   loaded, indefinitely, with no signal that it is stale — on 2026-07-31 a
   client change sat unloaded in an open tab for six hours while the server
   half of the same change was live, which read as a rendering bug rather
   than a stale page. The interval below is what closes that window; the
   toast is only the visible half of the fix. */

import { registerSW } from "virtual:pwa-register";

/** How often an open tab asks whether a newer worker has been deployed. */
const UPDATE_CHECK_MS = 30 * 60 * 1000; // 30 minutes

type Listener = (waiting: boolean) => void;

let updateWaiting = false;
const listeners = new Set<Listener>();

const updateSW = registerSW({
  immediate: true,
  onNeedRefresh: () => {
    updateWaiting = true;
    for (const listener of listeners) listener(true);
  },
  onRegisteredSW: (_swUrl, registration) => {
    if (registration === undefined) return;
    setInterval(() => {
      // Rejects when offline; there is nothing to do about that but retry
      // on the next tick.
      void registration.update().catch(() => undefined);
    }, UPDATE_CHECK_MS);
  },
});

/** Subscribe to "a newer build is installed and waiting". Returns unsubscribe.
    Fires immediately if the update landed before this listener attached. */
export function onUpdateWaiting(listener: Listener): () => void {
  listeners.add(listener);
  if (updateWaiting) listener(true);
  return () => {
    listeners.delete(listener);
  };
}

/** Accept the update: the waiting worker takes over and the page reloads. */
export function applyUpdate(): void {
  void updateSW(true);
}
