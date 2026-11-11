/* The stores that hold one account's data, and the one place they are emptied.
   A change of signed-in account resets them all before the next account's
   first request, so nothing the previous account loaded is drawn for it. */

import type { StoreApi } from "zustand";

import { abandonPendingRequests } from "../api";
import { useApprovals } from "./approvals";
import { useApps } from "./apps";
import { useChannelDelete } from "./channelDelete";
import { useChannels } from "./channels";
import { useMembers } from "./members";
import { useMembership } from "./membership";
import { useMessages } from "./messages";
import { usePresence } from "./presence";
import { useSession } from "./session";
import { useUnfurl } from "./unfurl";

export interface PerUserStore {
  getState: () => unknown;
  getInitialState: () => unknown;
  reset: () => void;
}

function perUser<T>(store: StoreApi<T>): PerUserStore {
  return {
    getState: store.getState,
    getInitialState: store.getInitialState,
    reset: () => store.setState(store.getInitialState(), true),
  };
}

/** Keyed by file name in this directory: every store here but the session is one account's. */
export const PER_USER_STORES: Record<string, PerUserStore> = {
  approvals: perUser(useApprovals),
  apps: perUser(useApps),
  channelDelete: perUser(useChannelDelete),
  channels: perUser(useChannels),
  members: perUser(useMembers),
  membership: perUser(useMembership),
  messages: perUser(useMessages),
  presence: perUser(usePresence),
  unfurl: perUser(useUnfurl),
};

export function resetPerUserStores(): void {
  // First, so an answer already on its way cannot refill a store just emptied.
  abandonPendingRequests();
  for (const store of Object.values(PER_USER_STORES)) store.reset();
}

useSession.subscribe((state, prev) => {
  if (state.user?.id !== prev.user?.id) resetPerUserStores();
});
