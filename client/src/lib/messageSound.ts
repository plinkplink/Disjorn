/* Which sound, if any, a live message_create should make. Pure: the socket
   gathers the facts, the sound module plays the answer. */

import { buildMentionRe } from "../components/Markdown";
import type { ChannelType, Message } from "../types";

export type SoundKind = "message" | "mention";

export interface SoundFacts {
  myUserId: number;
  /** My username and display name, the names an @mention of me can use. */
  myNames: string[];
  /** Undefined when the sidebar does not list the channel yet. */
  channelType: ChannelType | undefined;
  /** The same test that marks the message read on arrival: active channel, window focused. */
  readOnArrival: boolean;
  mentionsOnly: boolean;
}

export function soundFor(message: Message, facts: SoundFacts): SoundKind | null {
  if (message.author_type === "user" && message.author_id === facts.myUserId) {
    return null;
  }
  if (facts.readOnArrival) return null;
  // The build modal is where its builder's replies are watched.
  if (facts.channelType === "app_build") return null;
  // An unlisted channel is a DM someone just opened with us (see onMessageCreate).
  const isDm = facts.channelType === "dm_1to1" || facts.channelType === undefined;
  if (isDm || buildMentionRe(facts.myNames)?.test(message.content) === true) {
    return "mention";
  }
  return facts.mentionsOnly ? null : "message";
}
