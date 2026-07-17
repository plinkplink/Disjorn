/* Payload shapes mirroring the server (server/app/models.py + routers).
   These are the client-side contract for WP10-12 — extend, don't fork. */

export type MemberType = "user" | "bot";
export type ChannelType = "main_feed" | "dm_1to1";
export type UserStatus = "online" | "idle" | "dnd" | "offline";
/** Statuses a user can pick; "offline" is derived (disconnect), never set. */
export type SettableStatus = Exclude<UserStatus, "offline">;

export interface User {
  id: number;
  username: string;
  display_name: string;
  avatar_path: string | null;
  status: UserStatus;
  is_admin: boolean;
  created_at: string;
}

export interface MessageAuthor {
  type: MemberType;
  id: number;
  name: string;
  username?: string; // users only
  avatar_path: string | null;
}

export interface Attachment {
  id: number;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  width: number | null;
  height: number | null;
  url: string | null; // signed media URL
}

export interface Message {
  id: number;
  channel_id: number;
  seq: number;
  author_type: MemberType;
  author_id: number;
  author: MessageAuthor;
  content: string;
  created_at: string;
  edited_at: string | null;
  deleted_at: string | null;
  reply_to_id: number | null;
  privacy_flags: Record<string, unknown>;
  emote_refs: unknown[];
  attachments: Attachment[];
}

/** Backfill (`?from_seq=`) returns deleted messages as tombstones. */
export interface Tombstone {
  id: number;
  seq: number;
  deleted: true;
}

export type BackfillItem = Message | Tombstone;

export function isTombstone(item: BackfillItem): item is Tombstone {
  return "deleted" in item && item.deleted === true;
}

export interface LastMessage {
  seq: number;
  snippet: string;
  author_type: MemberType;
  author_id: number;
  created_at: string;
}

export interface ChannelListItem {
  id: number;
  type: ChannelType;
  /** main_feed: channel name; DMs: the OTHER participant's display name. */
  name: string | null;
  /** DMs only: the OTHER participant's user id. */
  dm_user_id: number | null;
  unread: number;
  last_message: LastMessage | null;
}

export interface DmResponse {
  id: number;
  type: ChannelType;
  name: string;
  dm_user_id: number;
  created: boolean;
}

export interface ChannelMemberOut {
  type: MemberType;
  id: number;
  name: string;
  status?: UserStatus | null; // users only
}

export interface SearchResult {
  message: Message;
  channel: { id: number; type: ChannelType; name: string | null };
}

/* ---- media / picker / unfurl / summarize (WP10) ---- */

/** POST /upload response item — richer than the in-message Attachment shape. */
export interface UploadedAttachment extends Attachment {
  message_id: number | null;
  has_preview: boolean;
  thumb_url: string;
  orig_url: string;
}

export interface UploadResponse {
  attachments: UploadedAttachment[];
  message: Message | null;
}

export interface PickerItem {
  name: string;
  url: string;
}

export interface UnfurlData {
  url: string;
  title: string | null;
  description: string | null;
  image_url: string | null;
}

export interface SummarizeResponse {
  url: string;
  summary: string;
}

/* ---- notifications / profile (WP11) ---- */

export interface NotifyPrefs {
  notify_all_main: boolean;
}

export interface AvatarUploadResponse {
  avatar_path: string;
  url: string;
}

/* ---- WebSocket frames (server -> client) ---- */

export interface ReadyFrame {
  type: "ready";
  user_id: number;
}

export interface MessageCreateFrame {
  type: "message_create";
  channel_id: number;
  seq: number;
  message: Message;
}

export interface MessageEditFrame {
  type: "message_edit";
  channel_id: number;
  seq: number;
  message: Message;
}

export interface MessageDeleteFrame {
  type: "message_delete";
  channel_id: number;
  id: number;
  seq: number;
}

export interface TypingStartFrame {
  type: "typing_start";
  channel_id: number;
  author_type: MemberType;
  author_id: number;
}

export interface PresenceFrame {
  type: "presence";
  user_id: number;
  status: UserStatus;
}

export type ServerFrame =
  | ReadyFrame
  | MessageCreateFrame
  | MessageEditFrame
  | MessageDeleteFrame
  | TypingStartFrame
  | PresenceFrame;

/* ---- Web Push payload (WP7 shape; consumed by src/sw.ts) ---- */

export interface PushPayload {
  title: string;
  body: string;
  channel_id: number;
  message_id: number;
  url: string; // e.g. "/channels/3"
}
