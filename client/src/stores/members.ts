import { create } from "zustand";

import { listMembers } from "../api";
import type { ChannelMemberOut, MemberType } from "../types";

/* Per-channel member roster — WP10 uses it to resolve typing-indicator names
   and to highlight @mentions. Loaded lazily per channel; live display-name
   changes are rare enough that a channel-switch refetch is fine. */

interface MembersState {
  byChannel: Record<number, ChannelMemberOut[]>;

  /** Fetch the roster once per channel (refetches silently on later calls). */
  ensureLoaded: (channelId: number) => Promise<void>;
  /** Force a refetch (WP11: member panel open, own profile rename). */
  refresh: (channelId: number) => Promise<void>;
  /** Refetch only if this channel's roster is already on screen/loaded — a
      member_add/remove about someone else must not fetch a roster we have no
      business asking for. */
  refreshIfLoaded: (channelId: number) => void;
  /** Forget a roster (membership revoked). */
  drop: (channelId: number) => void;
  nameFor: (channelId: number, type: MemberType, id: number) => string | null;
  /** Best-effort display name for a user from ANY loaded roster — the client's
      only user directory (the main-feed roster lists every user). Null when we
      have never seen them. */
  userName: (userId: number) => string | null;
}

const inFlight = new Set<number>();

export const useMembers = create<MembersState>()((set, get) => ({
  byChannel: {},

  ensureLoaded: async (channelId) => {
    if (get().byChannel[channelId] !== undefined || inFlight.has(channelId)) {
      return;
    }
    await get().refresh(channelId);
  },

  refresh: async (channelId) => {
    if (inFlight.has(channelId)) return;
    inFlight.add(channelId);
    try {
      const members = await listMembers(channelId);
      set({ byChannel: { ...get().byChannel, [channelId]: members } });
    } catch {
      /* roster is a nicety — typing lines fall back to "Someone" */
    } finally {
      inFlight.delete(channelId);
    }
  },

  refreshIfLoaded: (channelId) => {
    if (get().byChannel[channelId] === undefined) return;
    void get().refresh(channelId);
  },

  drop: (channelId) => {
    const next = { ...get().byChannel };
    delete next[channelId];
    set({ byChannel: next });
  },

  nameFor: (channelId, type, id) => {
    const members = get().byChannel[channelId];
    const hit = members?.find((m) => m.type === type && m.id === id);
    return hit?.name ?? null;
  },

  userName: (userId) => {
    for (const members of Object.values(get().byChannel)) {
      const hit = members.find((m) => m.type === "user" && m.id === userId);
      if (hit !== undefined) return hit.name;
    }
    return null;
  },
}));
