import { create } from "zustand";

import { deleteChannel as apiDelete } from "../api";
import type { ChannelDeleteFrame } from "../types";
import { useChannels } from "./channels";
import { forgetChannel } from "./membership";
import { useSession } from "./session";

/* Channel deletion, client side.

   Two paths reach the same place, and both have to be safe on their own:

   1. The DELETE this tab made — torn down as soon as it succeeds, because the
      row must not stay clickable while the frame is in flight.
   2. The `channel_delete` frame, which the server sends to EVERYONE who could
      see the channel, the deleter included. So the deleter's tab runs the
      teardown twice; forgetChannel is idempotent, and the notice is the only
      part that would be wrong to repeat.

   The notice is deliberately not the MembershipNotice modal: a channel
   vanishing is Discord's quietest event — the row just goes. The only person
   owed a word is someone who was reading it at that moment, and they get a
   corner toast, not a dialog. */

export interface DeletedChannelNotice {
  channelId: number;
  channelName: string;
  isPrivate: boolean;
}

interface ChannelDeleteState {
  /** "#name was deleted" — set only for a viewer who was looking at it. */
  notice: DeletedChannelNotice | null;
  dismissNotice: () => void;

  /** Delete a channel (owner/admin). Rejects with the server's ApiError so
      the modal can show `detail` verbatim. */
  remove: (channelId: number) => Promise<void>;

  /** WS channel_delete. */
  onChannelDelete: (frame: ChannelDeleteFrame) => void;
}

/**
 * Channels this tab is deleting itself.
 *
 * `by_user_id` already says who did it, but our own teardown runs before the
 * frame arrives — this is what keeps the frame from raising a "was deleted"
 * toast at the person who pressed the button, even if the frame's actor field
 * ever goes missing.
 */
const selfDeleting = new Set<number>();

export const useChannelDelete = create<ChannelDeleteState>()((set) => ({
  notice: null,

  dismissNotice: () => set({ notice: null }),

  remove: async (channelId) => {
    selfDeleting.add(channelId);
    try {
      await apiDelete(channelId);
    } catch (err) {
      selfDeleting.delete(channelId);
      throw err;
    }
    // Don't wait for the frame: every read path for this channel is a 404
    // from this moment on.
    forgetChannel(channelId);
  },

  onChannelDelete: (frame) => {
    const me = useSession.getState().user;
    const channels = useChannels.getState();
    // Asked BEFORE the teardown: our own delete already navigated away, which
    // is a second reason the deleter never sees the toast.
    const wasWatching = channels.activeChannelId === frame.channel_id;
    const mine =
      selfDeleting.has(frame.channel_id) ||
      (me !== null && frame.by_user_id === me.id);
    selfDeleting.delete(frame.channel_id);
    forgetChannel(frame.channel_id);
    if (mine || !wasWatching) return;
    set({
      notice: {
        channelId: frame.channel_id,
        channelName: frame.channel.name,
        isPrivate: frame.channel.visibility === "private",
      },
    });
  },
}));
