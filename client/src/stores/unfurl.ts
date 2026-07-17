import { create } from "zustand";

import { fetchUnfurl } from "../api";
import type { UnfurlData } from "../types";

/* Client-side unfurl cache. One fetch per URL per session; the server holds
   the durable 7-day cache, this map just stops re-render refetch storms. */

export interface UnfurlEntry {
  status: "loading" | "done" | "error";
  data: UnfurlData | null;
}

interface UnfurlState {
  byUrl: Record<string, UnfurlEntry>;

  /** Kick off a fetch for a URL if we've never seen it. Idempotent. */
  ensure: (url: string) => void;
}

export const useUnfurl = create<UnfurlState>()((set, get) => ({
  byUrl: {},

  ensure: (url) => {
    if (get().byUrl[url] !== undefined) return;
    set({ byUrl: { ...get().byUrl, [url]: { status: "loading", data: null } } });
    fetchUnfurl(url).then(
      (data) =>
        set({ byUrl: { ...get().byUrl, [url]: { status: "done", data } } }),
      () =>
        set({ byUrl: { ...get().byUrl, [url]: { status: "error", data: null } } }),
    );
  },
}));
