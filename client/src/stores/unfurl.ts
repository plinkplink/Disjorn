import { create } from "zustand";

import { fetchUnfurl } from "../api";
import type { UnfurlData } from "../types";

/* Client-side unfurl cache. The server holds the durable cache; this map
   stops re-render refetch storms. An entry that drew no card is fetched again
   on a backoff, and an image the browser could not load asks the server for a
   fresh unfurl, so neither failure lasts the session. */

export interface UnfurlEntry {
  status: "loading" | "done" | "error";
  data: UnfurlData | null;
  /** Fetches in a row that produced no card. */
  misses: number;
  /** Epoch ms from which a card-less entry is fetched again; null when it has a card. */
  retryAt: number | null;
  /** An image_url the browser failed to load, and when it last cost a server re-fetch. */
  deadImage: string | null;
  deadAt: number;
}

const RETRY_BASE_MS = 30_000;
const RETRY_MAX_MS = 30 * 60_000;
/** Matches the server's REFRESH_MIN_AGE: asking sooner gets the same row back. */
export const DEAD_IMAGE_MS = 10 * 60_000;

const UNSEEN: UnfurlEntry = {
  status: "loading",
  data: null,
  misses: 0,
  retryAt: null,
  deadImage: null,
  deadAt: 0,
};

export function retryDelayMs(misses: number): number {
  return Math.min(RETRY_BASE_MS * 4 ** Math.max(misses - 1, 0), RETRY_MAX_MS);
}

function hasCard(data: UnfurlData | null): boolean {
  return (
    data !== null &&
    (data.title !== null || data.description !== null || data.image_url !== null)
  );
}

export function shouldFetch(entry: UnfurlEntry | undefined, now: number): boolean {
  if (entry === undefined) return true;
  return entry.status !== "loading" && entry.retryAt !== null && now >= entry.retryAt;
}

/** The entry after a fetch answered `data`, or failed (null) and keeps what it had. */
export function settled(
  prev: UnfurlEntry | undefined,
  data: UnfurlData | null,
  now: number,
): UnfurlEntry {
  const card = hasCard(data);
  const misses = card ? 0 : (prev?.misses ?? 0) + 1;
  return {
    ...(prev ?? UNSEEN),
    status: data === null ? "error" : "done",
    data: data ?? prev?.data ?? null,
    misses,
    retryAt: card ? null : now + retryDelayMs(misses),
  };
}

/** The image a card may draw: none while the browser's failure to load it is recent. */
export function cardImage(entry: UnfurlEntry, now: number): string | null {
  const image = entry.data?.image_url ?? null;
  const dead = image === entry.deadImage && now - entry.deadAt < DEAD_IMAGE_MS;
  return dead ? null : image;
}

interface UnfurlState {
  byUrl: Record<string, UnfurlEntry>;

  /** Fetch a URL never seen, or one whose card-less entry is due a retry. Idempotent. */
  ensure: (url: string) => void;

  /** The browser could not load `src`, the image of this URL's card. */
  imageFailed: (url: string, src: string) => void;
}

export const useUnfurl = create<UnfurlState>()((set, get) => {
  const put = (url: string, entry: UnfurlEntry) =>
    set({ byUrl: { ...get().byUrl, [url]: entry } });

  return {
    byUrl: {},

    ensure: (url) => {
      const prev = get().byUrl[url];
      if (!shouldFetch(prev, Date.now())) return;
      // A retry keeps the data it had: a playing video card must not unmount.
      put(url, { ...(prev ?? UNSEEN), status: "loading", retryAt: null });
      fetchUnfurl(url).then(
        (data) => put(url, settled(get().byUrl[url], data, Date.now())),
        () => put(url, settled(get().byUrl[url], null, Date.now())),
      );
    },

    imageFailed: (url, src) => {
      const prev = get().byUrl[url];
      if (prev === undefined) return;
      const now = Date.now();
      const refresh = now - prev.deadAt >= DEAD_IMAGE_MS;
      put(url, { ...prev, deadImage: src, deadAt: refresh ? now : prev.deadAt });
      if (!refresh) return;
      fetchUnfurl(url, true).then(
        (data) => {
          const cur = get().byUrl[url];
          // A refresh that found nothing keeps the card already on screen.
          if (cur !== undefined && hasCard(data)) put(url, { ...cur, data });
        },
        () => undefined,
      );
    },
  };
});
