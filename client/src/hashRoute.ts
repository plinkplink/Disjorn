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
