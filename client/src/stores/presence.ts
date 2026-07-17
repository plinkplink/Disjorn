import { create } from "zustand";

import type { MemberType, UserStatus } from "../types";

export interface Typist {
  authorType: MemberType;
  authorId: number;
  /** Epoch ms after which this entry is stale. */
  expiresAt: number;
}

export const TYPING_TTL_MS = 4000;

interface PresenceState {
  /** Live user statuses from presence frames (fallback: channel/member data). */
  statuses: Record<number, UserStatus>;
  /** channelId -> active typists (pruned on decay). */
  typing: Record<number, Typist[]>;

  setStatus: (userId: number, status: UserStatus) => void;
  /** Record a typing_start; entry decays after TYPING_TTL_MS. */
  typingStarted: (
    channelId: number,
    authorType: MemberType,
    authorId: number,
  ) => void;
  statusOf: (userId: number) => UserStatus;
  typistsFor: (channelId: number) => Typist[];
}

let pruneTimer: ReturnType<typeof setTimeout> | null = null;

export const usePresence = create<PresenceState>()((set, get) => {
  const prune = () => {
    pruneTimer = null;
    const now = Date.now();
    const typing: Record<number, Typist[]> = {};
    let nextExpiry = Infinity;
    for (const [cid, typists] of Object.entries(get().typing)) {
      const alive = typists.filter((t) => t.expiresAt > now);
      if (alive.length > 0) {
        typing[Number(cid)] = alive;
        for (const t of alive) nextExpiry = Math.min(nextExpiry, t.expiresAt);
      }
    }
    set({ typing });
    if (nextExpiry < Infinity) {
      pruneTimer = setTimeout(prune, Math.max(nextExpiry - Date.now(), 50));
    }
  };

  return {
    statuses: {},
    typing: {},

    setStatus: (userId, status) =>
      set({ statuses: { ...get().statuses, [userId]: status } }),

    typingStarted: (channelId, authorType, authorId) => {
      const entry: Typist = {
        authorType,
        authorId,
        expiresAt: Date.now() + TYPING_TTL_MS,
      };
      const current = get().typing[channelId] ?? [];
      const rest = current.filter(
        (t) => !(t.authorType === authorType && t.authorId === authorId),
      );
      set({ typing: { ...get().typing, [channelId]: [...rest, entry] } });
      if (pruneTimer === null) {
        pruneTimer = setTimeout(prune, TYPING_TTL_MS + 50);
      }
    },

    statusOf: (userId) => get().statuses[userId] ?? "offline",
    typistsFor: (channelId) => get().typing[channelId] ?? [],
  };
});
