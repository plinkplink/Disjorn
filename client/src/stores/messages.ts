import { create } from "zustand";

import { fetchBackfill, fetchHistory } from "../api";
import { isTombstone } from "../types";
import type { Message } from "../types";

/** Per-channel message list, always ordered ascending by seq, deduped by seq. */
export interface ChannelMessages {
  /** Ascending by seq. */
  list: Message[];
  /** True once the oldest page has been reached (fetchOlder returned short). */
  reachedStart: boolean;
  /** True after the first history load for this channel. */
  loaded: boolean;
}

const HISTORY_PAGE = 50;

const emptyChannel = (): ChannelMessages => ({
  list: [],
  reachedStart: false,
  loaded: false,
});

/** Insert/replace by seq, preserving ascending order. Idempotent. */
function upsertBySeq(list: Message[], message: Message): Message[] {
  // Binary search for the insertion point.
  let lo = 0;
  let hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const item = list[mid];
    if (item !== undefined && item.seq < message.seq) lo = mid + 1;
    else hi = mid;
  }
  const existing = list[lo];
  if (existing !== undefined && existing.seq === message.seq) {
    const next = [...list];
    next[lo] = message;
    return next;
  }
  return [...list.slice(0, lo), message, ...list.slice(lo)];
}

/** A pending scroll-to-message request (search result click). MessageList for
    the target channel consumes it once the message row exists. */
export interface JumpTarget {
  channelId: number;
  messageId: number;
  /** Monotonic — re-requesting the same message still retriggers the effect. */
  nonce: number;
}

interface MessagesState {
  byChannel: Record<number, ChannelMessages>;
  jumpTarget: JumpTarget | null;

  /* -- WS event application (idempotent by seq/id) -- */
  applyCreate: (message: Message) => void;
  applyEdit: (message: Message) => void;
  applyDelete: (channelId: number, messageId: number, seq: number) => void;

  /* -- history / backfill -- */
  /** Initial load for a channel (no-op if already loaded). */
  ensureLoaded: (channelId: number) => Promise<void>;
  /** Older page via before_seq; sets reachedStart when exhausted. */
  fetchOlder: (channelId: number) => Promise<void>;
  /**
   * Reconnect resync: fetch everything from the last local seq + 1, applying
   * edits (payloads) and deletions (tombstones). Loops pages until caught up.
   */
  backfill: (channelId: number) => Promise<void>;
  /**
   * Search jump: make sure `message` (by id/seq) is in the loaded window,
   * fetching a history page around its seq if not. May leave a seq gap
   * between the fetched window and the live window — acceptable for jump
   * viewing; scrollback from the oldest edge still paginates contiguously.
   */
  ensureAround: (channelId: number, messageId: number, seq: number) => Promise<void>;

  /* -- jump-to-message (search results) -- */
  requestJump: (channelId: number, messageId: number) => void;
  clearJump: () => void;

  /* -- queries -- */
  lastSeq: (channelId: number) => number;
  channelIdsWithMessages: () => number[];
}

export const useMessages = create<MessagesState>()((set, get) => {
  const update = (
    channelId: number,
    fn: (cm: ChannelMessages) => ChannelMessages,
  ) => {
    const current = get().byChannel[channelId] ?? emptyChannel();
    set({ byChannel: { ...get().byChannel, [channelId]: fn(current) } });
  };

  return {
    byChannel: {},
    jumpTarget: null,

    applyCreate: (message) => {
      update(message.channel_id, (cm) => {
        // Ignore live messages for channels whose history was never loaded —
        // ensureLoaded will fetch them; inserting now would leave a gap below.
        if (!cm.loaded) return cm;
        return { ...cm, list: upsertBySeq(cm.list, message) };
      });
    },

    applyEdit: (message) => {
      update(message.channel_id, (cm) => {
        if (!cm.loaded) return cm;
        // Only replace if we actually hold it (edit of a message outside the
        // loaded window is not an insert).
        const held = cm.list.some((m) => m.id === message.id);
        return held ? { ...cm, list: upsertBySeq(cm.list, message) } : cm;
      });
    },

    applyDelete: (channelId, messageId, _seq) => {
      update(channelId, (cm) => ({
        ...cm,
        list: cm.list.filter((m) => m.id !== messageId),
      }));
    },

    ensureLoaded: async (channelId) => {
      if (get().byChannel[channelId]?.loaded) return;
      const page = await fetchHistory(channelId, { limit: HISTORY_PAGE });
      update(channelId, (cm) => {
        let list = cm.list;
        for (const message of page) list = upsertBySeq(list, message);
        return {
          ...cm,
          list,
          loaded: true,
          reachedStart: page.length < HISTORY_PAGE,
        };
      });
    },

    fetchOlder: async (channelId) => {
      const cm = get().byChannel[channelId];
      if (cm === undefined || cm.reachedStart) return;
      const oldest = cm.list[0];
      const page = await fetchHistory(channelId, {
        beforeSeq: oldest?.seq,
        limit: HISTORY_PAGE,
      });
      update(channelId, (current) => {
        let list = current.list;
        for (const message of page) list = upsertBySeq(list, message);
        return {
          ...current,
          list,
          reachedStart: page.length < HISTORY_PAGE,
        };
      });
    },

    backfill: async (channelId) => {
      const PAGE = 200;
      // Loop pages until a short page says we're caught up.
      for (;;) {
        const fromSeq = get().lastSeq(channelId) + 1;
        const items = await fetchBackfill(channelId, fromSeq, PAGE);
        update(channelId, (cm) => {
          let list = cm.list;
          for (const item of items) {
            if (isTombstone(item)) {
              list = list.filter((m) => m.id !== item.id);
            } else {
              list = upsertBySeq(list, item);
            }
          }
          return { ...cm, list };
        });
        if (items.length < PAGE) break;
        // Tombstone-heavy pages can leave lastSeq unchanged; use the page's
        // own max seq to guarantee forward progress.
        const maxSeq = items.reduce((acc, item) => Math.max(acc, item.seq), 0);
        if (maxSeq < fromSeq) break;
      }
    },

    ensureAround: async (channelId, messageId, seq) => {
      await get().ensureLoaded(channelId);
      const held = get()
        .byChannel[channelId]?.list.some((m) => m.id === messageId);
      if (held === true) return;
      // Window centered-ish on the target: newest-first page starting a few
      // messages after it, so the target lands with context on both sides.
      const page = await fetchHistory(channelId, {
        beforeSeq: seq + 10,
        limit: HISTORY_PAGE,
      });
      update(channelId, (cm) => {
        let list = cm.list;
        for (const message of page) list = upsertBySeq(list, message);
        return { ...cm, list };
      });
    },

    requestJump: (channelId, messageId) => {
      const prev = get().jumpTarget;
      set({
        jumpTarget: { channelId, messageId, nonce: (prev?.nonce ?? 0) + 1 },
      });
    },

    clearJump: () => set({ jumpTarget: null }),

    lastSeq: (channelId) => {
      const list = get().byChannel[channelId]?.list;
      const last = list?.[list.length - 1];
      return last?.seq ?? 0;
    },

    channelIdsWithMessages: () =>
      Object.entries(get().byChannel)
        .filter(([, cm]) => cm.list.length > 0)
        .map(([id]) => Number(id)),
  };
});
