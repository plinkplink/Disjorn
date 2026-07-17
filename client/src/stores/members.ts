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
  nameFor: (channelId: number, type: MemberType, id: number) => string | null;
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

  nameFor: (channelId, type, id) => {
    const members = get().byChannel[channelId];
    const hit = members?.find((m) => m.type === type && m.id === id);
    return hit?.name ?? null;
  },
}));
