import { create } from "zustand";

import { listChannels, markRead as apiMarkRead, openDm as apiOpenDm } from "../api";
import type { ChannelListItem, Message } from "../types";

function sortChannels(channels: ChannelListItem[]): ChannelListItem[] {
  // main_feed pinned first; DMs by most recent activity (server order mirrors
  // this — re-sorting locally keeps live updates consistent).
  const main = channels.filter((c) => c.type === "main_feed");
  const dms = channels
    .filter((c) => c.type !== "main_feed")
    .sort((a, b) =>
      (b.last_message?.created_at ?? "").localeCompare(
        a.last_message?.created_at ?? "",
      ),
    );
  return [...main, ...dms];
}

interface ChannelsState {
  channels: ChannelListItem[];
  activeChannelId: number | null;
  loaded: boolean;

  /** Fetch GET /channels (initial load and WS reconnect resync). */
  refresh: () => Promise<void>;
  /** Switch channels. Side effects (hash, focus op, mark-read) live in AppShell/ws. */
  setActive: (channelId: number | null) => void;
  /** Open (or create) the 1:1 DM with a user; returns the channel id. */
  openDm: (userId: number) => Promise<number>;
  /** Persist read state and zero the local unread badge. */
  markRead: (channelId: number, seq: number) => Promise<void>;
  /** Live update from a message_create frame: unread badge + last_message + order. */
  onMessageCreate: (message: Message, isRead: boolean) => void;
}

export const useChannels = create<ChannelsState>()((set, get) => ({
  channels: [],
  activeChannelId: null,
  loaded: false,

  refresh: async () => {
    const channels = await listChannels();
    set({ channels, loaded: true });
  },

  setActive: (channelId) => set({ activeChannelId: channelId }),

  openDm: async (userId) => {
    const existing = get().channels.find((c) => c.dm_user_id === userId);
    if (existing !== undefined) {
      set({ activeChannelId: existing.id });
      return existing.id;
    }
    const dm = await apiOpenDm(userId);
    await get().refresh(); // pull the fresh channel row into the sidebar
    set({ activeChannelId: dm.id });
    return dm.id;
  },

  markRead: async (channelId, seq) => {
    set({
      channels: get().channels.map((c) =>
        c.id === channelId ? { ...c, unread: 0 } : c,
      ),
    });
    try {
      await apiMarkRead(channelId, seq);
    } catch {
      /* badge will self-correct on next refresh */
    }
  },

  onMessageCreate: (message, isRead) => {
    // A message for a channel we don't know = a DM someone just opened with
    // us. Refetch the sidebar so the new row (with server-side unread) shows.
    if (!get().channels.some((c) => c.id === message.channel_id)) {
      void get().refresh();
      return;
    }
    const channels = get().channels.map((c) => {
      if (c.id !== message.channel_id) return c;
      return {
        ...c,
        unread: isRead ? 0 : c.unread + 1,
        last_message: {
          seq: message.seq,
          snippet:
            message.content.length > 80
              ? `${message.content.slice(0, 79)}…`
              : message.content,
          author_type: message.author_type,
          author_id: message.author_id,
          created_at: message.created_at,
        },
      };
    });
    set({ channels: sortChannels(channels) });
  },
}));
