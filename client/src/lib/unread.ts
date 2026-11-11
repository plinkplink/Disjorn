/* Unread bookkeeping. Pure: a channel's count is the server's count as of the
   last channel list plus the live arrivals above it, keyed by seq, so a frame
   the list already counted and a list older than a local read cannot skew it. */

import type { ChannelListItem } from "../types";

export interface UnreadState {
  /** The server's count as of the last channel list, for seqs up to `through`. */
  base: number;
  through: number;
  /** Unread seqs above `through` that arrived live. */
  live: number[];
  /** Highest seq this client has marked read. */
  read: number;
  /** Highest seq known to exist, deleted or not. */
  top: number;
}

const NONE: UnreadState = { base: 0, through: 0, live: [], read: 0, top: 0 };

export function unreadCount(state: UnreadState | undefined): number {
  return state === undefined ? 0 : state.base + state.live.length;
}

/** Fold a GET /channels row into what this client already knows. */
export function listed(
  prev: UnreadState | undefined,
  row: Pick<ChannelListItem, "unread" | "last_message">,
): UnreadState {
  const s = prev ?? NONE;
  const through = row.last_message?.seq ?? 0;
  return {
    // The list may have been computed before a read of ours reached the server.
    base: s.read >= through ? 0 : row.unread,
    through,
    live: s.live.filter((seq) => seq > through),
    read: s.read,
    top: Math.max(s.top, through),
  };
}

/** A live message_create. `readNow`: the reader is on it, or wrote it. */
export function created(
  prev: UnreadState | undefined,
  seq: number,
  readNow: boolean,
): UnreadState {
  const s = prev ?? NONE;
  const top = Math.max(s.top, seq);
  if (readNow) {
    const read = Math.max(s.read, seq);
    return {
      ...s,
      base: read >= s.through ? 0 : s.base,
      live: s.live.filter((x) => x > read),
      read,
      top,
    };
  }
  if (seq <= s.through || seq <= s.read || s.live.includes(seq)) {
    return { ...s, top };
  }
  return { ...s, live: [...s.live, seq], top };
}

/** A live message_delete: only an arrival counted here can be uncounted. */
export function deleted(
  prev: UnreadState | undefined,
  seq: number,
): UnreadState | undefined {
  if (prev === undefined || !prev.live.includes(seq)) return prev;
  return { ...prev, live: prev.live.filter((x) => x !== seq) };
}

/** The reader is at the bottom of the channel: everything known is read. */
export function markedRead(
  prev: UnreadState | undefined,
  seq: number,
): UnreadState {
  const s = prev ?? NONE;
  const read = Math.max(s.read, seq, s.top);
  return { ...s, base: 0, live: [], read, top: read };
}

/** A read the server never stored must not hide the next list's count. */
export function readRefused(
  state: UnreadState | undefined,
  attempted: number,
  previous: number,
): UnreadState | undefined {
  if (state === undefined || state.read !== attempted) return state;
  return { ...state, read: previous };
}

/** A build chat's only door is its modal, so it is never a sidebar row. */
export function isSidebarRow(row: Pick<ChannelListItem, "type">): boolean {
  return row.type !== "app_build";
}

export function totalUnread(
  rows: ReadonlyArray<Pick<ChannelListItem, "type" | "unread">>,
): number {
  return rows.reduce((sum, r) => (isSidebarRow(r) ? sum + r.unread : sum), 0);
}

export function tabTitle(
  rows: ReadonlyArray<Pick<ChannelListItem, "type" | "unread">>,
): string {
  const total = totalUnread(rows);
  return total > 0 ? `(${total}) Disjorn` : "Disjorn";
}
