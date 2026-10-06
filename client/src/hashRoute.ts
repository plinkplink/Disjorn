/* Tiny hash <-> activeChannel sync (#/channels/{id}). No router — just enough
   for notification deep-links and refresh-safety. AppShell wires it up. */

const CHANNEL_HASH_RE = /^#\/channels\/(\d+)$/;

export function channelIdFromHash(hash: string = location.hash): number | null {
  const match = CHANNEL_HASH_RE.exec(hash);
  const id = match?.[1];
  return id !== undefined ? Number(id) : null;
}

export function writeChannelHash(channelId: number | null): void {
  const next = channelId === null ? "" : `#/channels/${channelId}`;
  if (location.hash === next) return;
  if (next === "") {
    // Strip the hash without adding a history entry.
    history.replaceState(null, "", location.pathname + location.search);
  } else {
    history.replaceState(null, "", next);
  }
}

/** Subscribe to hash changes; returns an unsubscribe function. */
export function onChannelHashChange(
  fn: (channelId: number | null) => void,
): () => void {
  const handler = () => fn(channelIdFromHash());
  window.addEventListener("hashchange", handler);
  return () => window.removeEventListener("hashchange", handler);
}

/* The Plan Room's own routes: #/planroom, #/planroom/approvals and
   #/planroom/approvals/<slug>. */

export type PlanRoomTab = "board" | "approvals";

export interface PlanRoomRoute {
  tab: PlanRoomTab;
  slug: string | null;
}

const PLANROOM_HASH_RE = /^#\/planroom(?:\/(approvals)(?:\/([^/]+))?)?\/?$/;

export function planRoomRouteFromHash(
  hash: string = location.hash,
): PlanRoomRoute | null {
  const match = PLANROOM_HASH_RE.exec(hash);
  if (match === null) return null;
  if (match[1] === undefined) return { tab: "board", slug: null };
  let slug: string | null = null;
  if (match[2] !== undefined) {
    try {
      slug = decodeURIComponent(match[2]);
    } catch {
      slug = null;
    }
  }
  return { tab: "approvals", slug };
}

export function planRoomHash(route: PlanRoomRoute): string {
  if (route.tab === "board") return "#/planroom";
  if (route.slug === null) return "#/planroom/approvals";
  return `#/planroom/approvals/${encodeURIComponent(route.slug)}`;
}

/** replaceState, like the rest of the app: its own navigation adds no history. */
export function writePlanRoomHash(route: PlanRoomRoute): void {
  const next = planRoomHash(route);
  if (location.hash !== next) history.replaceState(null, "", next);
}
