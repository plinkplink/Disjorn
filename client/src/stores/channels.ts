import { create } from "zustand";

import {
  ApiError,
  listChannels,
  markRead as apiMarkRead,
  openBotDm as apiOpenBotDm,
  openDm as apiOpenDm,
} from "../api";
import {
  created,
  deleted,
  listed,
  markedRead,
  readRefused,
  unreadCount,
} from "../lib/unread";
import type { UnreadState } from "../lib/unread";
import type { ChannelListItem, ChannelVisibility, Message } from "../types";

/** Backoff between first-load attempts that got no answer or a 5xx. */
const LOAD_RETRY_DELAYS_MS = [500, 1500, 4000];

function retryable(err: unknown): boolean {
  return !(err instanceof ApiError) || err.status === 0 || err.status >= 500;
}

let firstLoad: Promise<void> | null = null;
let listsRequested = 0;
let listApplied = 0;

function sortChannels(channels: ChannelListItem[]): ChannelListItem[] {
  // main_feed pinned first; text channels alphabetically; DMs by most recent
  // activity (server order mirrors this — re-sorting locally keeps live
  // updates consistent).
  const main = channels.filter((c) => c.type === "main_feed");
  const texts = channels
    .filter((c) => c.type === "text")
    .sort((a, b) => (a.name ?? "").localeCompare(b.name ?? ""));
  const dms = channels
    .filter((c) => c.type === "dm_1to1")
    .sort((a, b) =>
      (b.last_message?.created_at ?? "").localeCompare(
        a.last_message?.created_at ?? "",
      ),
    );
  // Rows the sidebar never draws (build chats) still carry state: keep them.
  const rest = channels.filter(
    (c) => c.type !== "main_feed" && c.type !== "text" && c.type !== "dm_1to1",
  );
  return [...main, ...texts, ...dms, ...rest];
}

interface ChannelsState {
  /** Each row's `unread` is unreadCount of its `unreadState` entry, never set apart from it. */
  channels: ChannelListItem[];
  unreadState: Record<number, UnreadState>;
  activeChannelId: number | null;
  loaded: boolean;
  /** The first load gave up; the shell offers a retry instead of an empty list. */
  loadFailed: boolean;

  /** Fetch GET /channels (initial load and WS reconnect resync). */
  refresh: () => Promise<void>;
  /** First load with backoff; no-op once loaded, shared while in flight. Never rejects. */
  ensureLoaded: () => Promise<void>;
  /** Switch channels. Side effects (hash, focus op, mark-read) live in AppShell/ws. */
  setActive: (channelId: number | null) => void;
  /** Open (or create) the 1:1 DM with a user; returns the channel id. */
  openDm: (userId: number) => Promise<number>;
  /** Open (or create) the 1:1 DM with a bot; returns the channel id. */
  openBotDm: (botId: number) => Promise<number>;
  /** Persist read state and zero the local unread badge. */
  markRead: (channelId: number, seq: number) => Promise<void>;
  /** Live update from a message_create frame: unread badge + last_message + order. */
  onMessageCreate: (message: Message, isRead: boolean) => void;
  /** Live update from a message_delete frame: an unread arrival stops counting. */
  onMessageDelete: (channelId: number, seq: number) => void;
  /** Live update from a channel_create frame: add the new text channel row. */
  onChannelCreate: (channel: {
    id: number;
    type: ChannelListItem["type"];
    name: string;
    visibility?: ChannelVisibility;
  }) => void;
  /** Drop a row we can no longer see (membership revoked / left). */
  removeChannel: (channelId: number) => void;
  /** The main feed's id — where the app falls back when a channel goes away. */
  mainFeedId: () => number | null;
}

function withUnread(
  state: ChannelsState,
  channelId: number,
  next: UnreadState,
): Pick<ChannelsState, "channels" | "unreadState"> {
  return {
    unreadState: { ...state.unreadState, [channelId]: next },
    channels: state.channels.map((c) =>
      c.id === channelId ? { ...c, unread: unreadCount(next) } : c,
    ),
  };
}

export const useChannels = create<ChannelsState>()((set, get) => ({
  channels: [],
  unreadState: {},
  activeChannelId: null,
  loaded: false,
  loadFailed: false,

  refresh: async () => {
    const request = ++listsRequested;
    const rows = await listChannels();
    // A list landing after a newer one would undo it.
    if (request < listApplied) return;
    listApplied = request;
    const known = get().unreadState;
    const unreadState: Record<number, UnreadState> = {};
    for (const row of rows) unreadState[row.id] = listed(known[row.id], row);
    const channels = rows.map((row) => ({
      ...row,
      unread: unreadCount(unreadState[row.id]),
    }));
    set({ channels, unreadState, loaded: true, loadFailed: false });
  },

  ensureLoaded: () => {
    if (get().loaded) return Promise.resolve();
    if (firstLoad !== null) return firstLoad;
    set({ loadFailed: false });
    firstLoad = (async () => {
      for (let attempt = 0; !get().loaded; attempt++) {
        try {
          await get().refresh();
          return;
        } catch (err) {
          if (!retryable(err) || attempt >= LOAD_RETRY_DELAYS_MS.length) {
            set({ loadFailed: true });
            return;
          }
          await new Promise((resolve) =>
            setTimeout(resolve, LOAD_RETRY_DELAYS_MS[attempt]),
          );
        }
      }
    })().finally(() => {
      firstLoad = null;
    });
    return firstLoad;
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

  openBotDm: async (botId) => {
    const existing = get().channels.find((c) => c.dm_bot_id === botId);
    if (existing !== undefined) {
      set({ activeChannelId: existing.id });
      return existing.id;
    }
    const dm = await apiOpenBotDm(botId);
    await get().refresh();
    set({ activeChannelId: dm.id });
    return dm.id;
  },

  markRead: async (channelId, seq) => {
    const before = get().unreadState[channelId];
    const next = markedRead(before, seq);
    set(withUnread(get(), channelId, next));
    try {
      await apiMarkRead(channelId, next.read);
    } catch {
      const now = readRefused(
        get().unreadState[channelId],
        next.read,
        before?.read ?? 0,
      );
      if (now !== undefined) set(withUnread(get(), channelId, now));
    }
  },

  onMessageCreate: (message, isRead) => {
    // A message for a channel we don't know = a DM someone just opened with
    // us. Refetch the sidebar so the new row (with server-side unread) shows.
    if (!get().channels.some((c) => c.id === message.channel_id)) {
      void get().refresh();
      return;
    }
    const next = created(
      get().unreadState[message.channel_id],
      message.seq,
      isRead,
    );
    const channels = get().channels.map((c) => {
      if (c.id !== message.channel_id) return c;
      return {
        ...c,
        unread: unreadCount(next),
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
    set({
      channels: sortChannels(channels),
      unreadState: { ...get().unreadState, [message.channel_id]: next },
    });
  },

  onMessageDelete: (channelId, seq) => {
    const before = get().unreadState[channelId];
    const next = deleted(before, seq);
    if (next !== undefined && next !== before) {
      set(withUnread(get(), channelId, next));
    }
  },

  onChannelCreate: (channel) => {
    // The creator may already have it via the post-create refresh — dedupe.
    if (get().channels.some((c) => c.id === channel.id)) return;
    // A private channel_create only reaches its members, so the row we would
    // synthesize is one we can read — but it also needs `created_by` (the
    // owner-only affordances key off it), which the frame does not carry.
    // Refetching is the honest way to learn it, and it happens once.
    if (channel.visibility === "private") {
      void get().refresh();
      return;
    }
    set({
      channels: sortChannels([
        ...get().channels,
        {
          id: channel.id,
          type: channel.type,
          name: channel.name,
          dm_user_id: null,
          unread: 0,
          last_message: null,
          visibility: channel.visibility ?? "public",
          member: true,
        },
      ]),
    });
  },

  removeChannel: (channelId) => {
    const unreadState = { ...get().unreadState };
    delete unreadState[channelId];
    set({
      channels: get().channels.filter((c) => c.id !== channelId),
      unreadState,
    });
  },

  mainFeedId: () => get().channels.find((c) => c.type === "main_feed")?.id ?? null,
}));
