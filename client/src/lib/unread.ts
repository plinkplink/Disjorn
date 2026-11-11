/* Unread bookkeeping. Pure: a channel's count is the server's count as of the
   last channel list plus the live arrivals above it, keyed by seq, so a frame
   the list already counted and a list older than a known read cannot skew it. */

import type { ChannelListItem } from "../types";

export interface UnreadState {
  /** The server's count as of the last channel list, for seqs up to `through`. */
  base: number;
  through: number;
  /** Unread seqs above `through` that arrived live. */
  live: number[];
  /** Highest seq known read: marked here, or the server's cursor. */
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
  row: Pick<ChannelListItem, "unread" | "last_message" | "last_read_seq">,
): UnreadState {
  const s = prev ?? NONE;
  const through = row.last_message?.seq ?? 0;
  const cursor = row.last_read_seq ?? 0;
  const top = Math.max(s.top, through);
  /* A list computed before a read known here counted messages already read.
     What was counted above that read stands; `through` stays put so a frame
     still in flight is counted when it lands. */
  if (cursor < s.read) return { ...s, top };
  return {
    base: row.unread,
    through,
    live: s.live.filter((seq) => seq > through),
    read: cursor,
    top,
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

/** The server's cursor reached `seq`, from any device; an older cursor changes nothing. */
export function readThrough(
  prev: UnreadState | undefined,
  seq: number,
): UnreadState | undefined {
  if (prev === undefined || seq <= prev.read) return prev;
  return {
    ...prev,
    // Below `through` the list's count spans the cursor, and stays an overcount.
    base: seq >= prev.through ? 0 : prev.base,
    live: prev.live.filter((x) => x > seq),
    read: seq,
  };
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

/** Where the reader is looking, as the shell and the message list report it. */
export interface ReadingFacts {
  windowFocused: boolean;
  /** The channel the shell has open. */
  activeChannelId: number | null;
  /** The channel the socket last reported as focused: none under Settings,
      the Plan Room or a blurred window. */
  focusedChannelId: number | null;
  /** The app build modal is drawn over the channel. */
  buildModalOpen: boolean;
  /** The channel's message list is mounted and scrolled to its bottom. */
  atBottom: boolean;
}

/** Whether a channel's newest message is in front of the reader. */
export function isReading(channelId: number, facts: ReadingFacts): boolean {
  return (
    facts.windowFocused &&
    facts.activeChannelId === channelId &&
    facts.focusedChannelId === channelId &&
    !facts.buildModalOpen &&
    facts.atBottom
  );
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

export type BadgeCall = "set" | "clear" | null;

/** What the installed app's icon badge needs for this total; null where the
    Badging API does not exist. */
export function badgeCall(total: number, supported: boolean): BadgeCall {
  if (!supported) return null;
  return total > 0 ? "set" : "clear";
}

export interface BadgeNavigator {
  setAppBadge?: (count?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
}

/** Put the total on the app icon. A badge is decoration: this never throws
    and never leaves a rejection unhandled. */
export function syncAppBadge(nav: BadgeNavigator | undefined, total: number): void {
  const supported =
    typeof nav?.setAppBadge === "function" && typeof nav.clearAppBadge === "function";
  const call = badgeCall(total, supported);
  if (call === null || nav === undefined) return;
  try {
    const done = call === "set" ? nav.setAppBadge?.(total) : nav.clearAppBadge?.();
    void Promise.resolve(done).catch(() => undefined);
  } catch {
    /* refused by the platform */
  }
}
