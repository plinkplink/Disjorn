/* Payload shapes mirroring the server (server/app/models.py + routers).
   These are the client-side contract for WP10-12 — extend, don't fork. */

export type MemberType = "user" | "bot";
export type ChannelType = "main_feed" | "dm_1to1" | "text";
/** Per-channel access mode. Everything created before the membership spec —
    and everything created without asking — is `public`. */
export type ChannelVisibility = "public" | "private";
export type UserStatus = "online" | "idle" | "dnd" | "offline";
/** Statuses a user can pick; "offline" is derived (disconnect), never set. */
export type SettableStatus = Exclude<UserStatus, "offline">;

/* `avatar_url` is the server's versioned serving URL — `/avatars/{id}?v={mtime}`
   for users, `/bots/{id}/avatar?v={mtime}` for bots (server media.py
   avatar_version). null means "no avatar, don't ask": the request would only
   404. The `?v=` is the file's mtime, so a repainted avatar arrives with a new
   URL instead of hiding behind the response cache. Optional (not just
   nullable) so a payload from before the server grew the field is still a
   valid object — such a payload just renders the letter tile. */

export interface User {
  id: number;
  username: string;
  display_name: string;
  avatar_path: string | null;
  avatar_url?: string | null;
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
  avatar_url?: string | null;
}

export interface Attachment {
  id: number;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  width: number | null;
  height: number | null;
  url: string | null; // signed media URL (display variant)
  /* The server grew thumb/orig variants on the message payload after the
     first clients shipped — optional here so a message from an older payload
     (or an older server) is still a valid Attachment. */
  thumb_url?: string | null;
  /** Preserved upload, pre-conversion. Absent -> no "view original". */
  orig_url?: string | null;
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
  /** main_feed/text: channel name; DMs: the OTHER participant's display name. */
  name: string | null;
  /** DMs only: the OTHER participant's user id. */
  dm_user_id: number | null;
  unread: number;
  last_message: LastMessage | null;
  /* The three fields below arrived with per-channel membership. All optional
     (not merely nullable) so a payload from a server that predates the spec is
     still a valid row — read them through isPrivateChannel/isChannelMember,
     never raw, so "absent" keeps meaning "public, and I'm in it". */
  visibility?: ChannelVisibility;
  /**
   * False ONLY for a private channel the caller is not a member of: the row is
   * listed (existence is not a secret) but carries no content, and every read
   * path for it answers 403. Absent => member.
   */
  member?: boolean;
  /** The owner — the one account that may invite/kick/add bots. Null for
      main_feed and DMs, which have no creator. */
  created_by?: number | null;
}

/** Private = the wall is up. Absent visibility means public (older payload). */
export function isPrivateChannel(channel: {
  visibility?: ChannelVisibility;
}): boolean {
  return channel.visibility === "private";
}

/** Am I in it? Only an explicit `false` means no — see ChannelListItem.member. */
export function isChannelMember(channel: { member?: boolean }): boolean {
  return channel.member !== false;
}

export interface DmResponse {
  id: number;
  type: ChannelType;
  name: string;
  dm_user_id: number;
  created: boolean;
}

/** Public bot shape (GET /bots) — never carries the API key. */
export interface Bot {
  id: number;
  name: string;
  avatar_path: string | null;
  avatar_url?: string | null;
  chibi_pack: string | null;
  created_at: string;
}

export interface ChannelMemberOut {
  type: MemberType;
  id: number;
  name: string;
  status?: UserStatus | null; // users only
  avatar_path?: string | null;
  avatar_url?: string | null;
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

/** POST /me/avatar. `url` is the freshly versioned `avatar_url` for the file
    just written — assign it straight onto the session user so every <img>
    rendered from then on points at the new bytes. */
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

/** A named text channel was created. Public: everyone. Private: members only
    (which, at creation time, is exactly its owner). */
export interface ChannelCreateFrame {
  type: "channel_create";
  channel: {
    id: number;
    type: ChannelType;
    name: string;
    visibility?: ChannelVisibility;
  };
}

/** Shared shape of member_add / member_remove. The subject of the event is
    always among the recipients — including a member_remove that names you,
    which is the last frame you get for that channel. */
interface MemberEventFrame {
  channel_id: number;
  member_type: MemberType;
  member_id: number;
  /**
   * Who did it: the inviter/kicker, the leaver themselves (member_remove where
   * by_user_id === member_id), or null/absent when no acting user is known
   * (older server, or a server-side action). Never assume it resolves.
   */
  by_user_id?: number | null;
  channel: {
    id: number;
    type: ChannelType;
    name: string | null;
    visibility?: ChannelVisibility;
  };
}

export interface MemberAddFrame extends MemberEventFrame {
  type: "member_add";
}

export interface MemberRemoveFrame extends MemberEventFrame {
  type: "member_remove";
}

export type ServerFrame =
  | ReadyFrame
  | MessageCreateFrame
  | MessageEditFrame
  | MessageDeleteFrame
  | TypingStartFrame
  | PresenceFrame
  | ChannelCreateFrame
  | MemberAddFrame
  | MemberRemoveFrame;

/* ---- Web Push payload (WP7 shape; consumed by src/sw.ts) ---- */

export interface PushPayload {
  title: string;
  body: string;
  channel_id: number;
  message_id: number;
  url: string; // e.g. "/channels/3"
}
