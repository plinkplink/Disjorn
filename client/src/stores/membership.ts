import { create } from "zustand";

import { leaveChannel as apiLeave } from "../api";
import type {
  ChannelVisibility,
  MemberAddFrame,
  MemberRemoveFrame,
} from "../types";
import { isChannelMember } from "../types";
import { useChannels } from "./channels";
import { useMembers } from "./members";
import { useMessages } from "./messages";
import { useSession } from "./session";

/* Per-channel membership, client side (SPECS/2026-08-08-per-channel-membership).

   Everything that happens TO my membership lands here: the two WS frames, and
   the one verb (leave) whose effect is on me. Two rules shape it:

   1. Losing membership must leave nothing readable behind — the sidebar row,
      the cached messages and the roster all go at once. The server would 403
      the next fetch anyway; this is about not showing yesterday's history in
      a room I was just removed from.
   2. A frame about SOMEONE ELSE never triggers a fetch of a roster we don't
      already hold (refreshIfLoaded), so a busy private channel can't turn
      into a burst of requests — or, worse, 403s — from a non-member's tab.

   The add/remove notice is a modal for now (plink's call). It is state, not a
   toast queue: one notice at a time, the newest wins. */

export interface MembershipNotice {
  kind: "added" | "removed";
  channelId: number;
  channelName: string;
  visibility: ChannelVisibility;
  /** Who did it, when the frame said (older frames may not). */
  byUserId: number | null;
}

interface MembershipState {
  notice: MembershipNotice | null;
  dismissNotice: () => void;

  /** WS member_add. */
  onMemberAdd: (frame: MemberAddFrame) => void;
  /** WS member_remove. */
  onMemberRemove: (frame: MemberRemoveFrame) => void;

  /** Leave a channel yourself. Applies the local removal immediately (the
      member_remove frame for it is idempotent with this). */
  leave: (channelId: number) => Promise<void>;

  /**
   * Reconnect housekeeping: forget every channel the freshly fetched list says
   * we can no longer read. Without it, a kick that happened while the socket
   * was down leaves cached messages on screen and sends the resync backfill
   * straight into a 403.
   */
  pruneUnreadable: () => void;
}

/**
 * Channels this tab is walking out of.
 *
 * `by_user_id` already distinguishes "you left" from "you were removed", but
 * it is optional in the frame and a null would otherwise accuse a stranger of
 * kicking you out of a room you left yourself. Remembering our own intent
 * makes the no-modal case not depend on a field that might be absent.
 */
const selfLeaving = new Set<number>();

/**
 * Drop every trace of a channel and step off it if it was on screen.
 *
 * Idempotent, and shared with the delete store: losing access and the channel
 * ceasing to exist have exactly the same local fallout, and it must happen
 * exactly once per channel however many times it is asked for.
 */
export function forgetChannel(channelId: number): void {
  const channels = useChannels.getState();
  channels.removeChannel(channelId);
  useMessages.getState().dropChannel(channelId);
  useMembers.getState().drop(channelId);
  if (channels.activeChannelId === channelId) {
    channels.setActive(channels.mainFeedId());
  }
}

export const useMembership = create<MembershipState>()((set) => ({
  notice: null,

  dismissNotice: () => set({ notice: null }),

  onMemberAdd: (frame) => {
    const me = useSession.getState().user;
    const aboutMe =
      frame.member_type === "user" && me !== null && frame.member_id === me.id;
    if (!aboutMe) {
      // Someone (or some bot) else joined a channel — only the panel that is
      // already showing that roster needs to hear about it.
      useMembers.getState().refreshIfLoaded(frame.channel_id);
      return;
    }
    // My own add: the row (and its content) is new to this client, so the
    // sidebar has to come from the server rather than be synthesized.
    void useChannels.getState().refresh();
    selfLeaving.delete(frame.channel_id);
    set({
      notice: {
        kind: "added",
        channelId: frame.channel_id,
        channelName: frame.channel.name ?? "channel",
        visibility: frame.channel.visibility ?? "private",
        byUserId: frame.by_user_id ?? null,
      },
    });
  },

  onMemberRemove: (frame) => {
    const me = useSession.getState().user;
    const aboutMe =
      frame.member_type === "user" && me !== null && frame.member_id === me.id;
    if (!aboutMe) {
      useMembers.getState().refreshIfLoaded(frame.channel_id);
      return;
    }
    const selfLeave =
      selfLeaving.has(frame.channel_id) ||
      (me !== null && frame.by_user_id === me.id);
    selfLeaving.delete(frame.channel_id);
    forgetChannel(frame.channel_id);
    if (selfLeave) return; // you know what you did — no modal
    set({
      notice: {
        kind: "removed",
        channelId: frame.channel_id,
        channelName: frame.channel.name ?? "channel",
        visibility: frame.channel.visibility ?? "private",
        byUserId: frame.by_user_id ?? null,
      },
    });
  },

  pruneUnreadable: () => {
    const readable = new Set(
      useChannels
        .getState()
        .channels.filter((c) => isChannelMember(c))
        .map((c) => c.id),
    );
    const held = new Set([
      ...useMessages.getState().channelIdsWithMessages(),
      ...Object.keys(useMembers.getState().byChannel).map(Number),
    ]);
    for (const channelId of held) {
      if (!readable.has(channelId)) forgetChannel(channelId);
    }
  },

  leave: async (channelId) => {
    selfLeaving.add(channelId);
    try {
      await apiLeave(channelId);
    } catch (err) {
      selfLeaving.delete(channelId);
      throw err;
    }
    // Don't wait for the frame: the row must not stay clickable, and every
    // read path for it is 403 from this moment on.
    forgetChannel(channelId);
  },
}));
