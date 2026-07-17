/* Custom service worker (vite-plugin-pwa injectManifest strategy).

   Precaches the built app shell (self.__WB_MANIFEST, injected at build time)
   with a hand-rolled cache — no workbox runtime dependency — and carries the
   Web Push plumbing for WP7's payload shape:
       event.data.json() -> {title, body, channel_id, message_id, url}
   WP11 wires the subscription UI; the handlers land here so pushes work the
   moment a subscription exists. Notification clicks deep-link via the url
   ("/channels/{id}" -> "/#/channels/{id}" hash route). */

import type { PushPayload } from "./types";

interface PrecacheEntry {
  url: string;
  revision: string | null;
}

declare let self: ServiceWorkerGlobalScope & {
  /** Injected by vite-plugin-pwa (injectManifest) at build time. */
  __WB_MANIFEST: Array<PrecacheEntry | string>;
};

const manifest = self.__WB_MANIFEST;

const CACHE_NAME = "disjorn-shell-v1";

// Manifest urls are scope-relative ("index.html", "assets/x.js"). Cache under
// those urls (addAll refetches on every SW update, so same-named files like
// index.html stay fresh); match against absolute pathnames.
const precacheUrls = manifest.map((entry) =>
  typeof entry === "string" ? entry : entry.url,
);
const precachePaths = new Set(
  precacheUrls.map((url) => new URL(url, self.location.origin).pathname),
);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(precacheUrls))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k.startsWith("disjorn-shell-") && k !== CACHE_NAME)
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // SPA navigations: network first, cached index.html offline.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(async () => {
        const cached =
          (await caches.match("/index.html")) ?? (await caches.match("/"));
        return cached ?? Response.error();
      }),
    );
    return;
  }

  // Precached static assets: cache first.
  if (precachePaths.has(url.pathname)) {
    event.respondWith(
      caches.match(request, { ignoreSearch: true }).then(
        (cached) => cached ?? fetch(request),
      ),
    );
  }
  // Everything else (API, media, avatars): straight to the network.
});

/* ---- Web Push (WP7 payload shape; WP11 adds the subscribe UI) ---- */

self.addEventListener("push", (event) => {
  if (event.data === null) return;
  let payload: PushPayload;
  try {
    payload = event.data.json() as PushPayload;
  } catch {
    return;
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      tag: `channel-${payload.channel_id}`, // newest per channel wins
      data: {
        url: payload.url,
        channel_id: payload.channel_id,
        message_id: payload.message_id,
      },
    }),
  );
});

/* Browser rotated/expired the subscription: re-subscribe with the same VAPID
   key and re-POST. Same-origin SW fetch sends the session cookie by default
   ("same-origin" credentials), so the server can attribute the new endpoint. */
interface PushSubscriptionChangeEvent extends ExtendableEvent {
  readonly oldSubscription: PushSubscription | null;
  readonly newSubscription: PushSubscription | null;
}

self.addEventListener("pushsubscriptionchange", (event) => {
  const e = event as PushSubscriptionChangeEvent;
  e.waitUntil(
    (async () => {
      try {
        let sub = e.newSubscription;
        if (sub === null) {
          const key = e.oldSubscription?.options.applicationServerKey;
          if (key == null) return; // nothing to re-subscribe with
          sub = await self.registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: key,
          });
        }
        const json = sub.toJSON();
        if (json.endpoint === undefined) return;
        await fetch("/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys ?? {} }),
        });
      } catch {
        // Session expired or subscribe failed — the Settings page re-syncs
        // the next time the app opens.
      }
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data as PushPayload | undefined;
  // Server sends "/channels/{id}"; our SPA routes via the hash.
  const target =
    data !== undefined && typeof data.url === "string" && data.url.length > 0
      ? `/#${data.url}`
      : "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(async (clients) => {
        const existing = clients[0];
        if (existing !== undefined) {
          await existing.focus();
          // Deep-link the already-open app (hashchange drives channel switch).
          await existing.navigate(target).catch(() => undefined);
          return;
        }
        await self.clients.openWindow(target);
      }),
  );
});
