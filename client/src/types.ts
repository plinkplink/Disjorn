/* Payload shapes mirroring the server (server/app/models.py + routers).
   These are the client-side contract for WP10-12 — extend, don't fork. */

export type MemberType = "user" | "bot";
/* `app_build` is the modal chat behind an app build session: exactly two
   members (the session owner and the builder resident), created by the server
   with the session. The sidebar never lists one — AppShell filters the type
   out of both channel groups — but GET /channels keeps returning it so
   reconnect resync and unread bookkeeping go on working (brief D9). */
export type ChannelType = "main_feed" | "dm_1to1" | "text" | "app_build";
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

/* ---- apps (SPECS/2026-08-30-apps-tab-v1.md, stage 1) ---- */

/* The stage vocabulary is FIXED and shared with the server's CHECK constraint
   (brief D7). Order is load-bearing: it is the stage bar, left to right. No
   percent exists anywhere — a stage is reached or it is not.

   It is also NOT the whole story, and stage 2 is where that starts to matter:
   a halted turn re-posts its last reached stage with `detail.halted` set, and
   a turn that changed nothing is `files_written` with `no_changes`. So a
   renderer keys its LABEL off the detail, never off the stage name
   (SPECS/2026-09-06-apps-builder-seat.md §A, Claudette #2293). */
export const APP_STAGES = [
  "scoped",
  "scaffolded",
  "files_written",
  "deployed",
  "live",
] as const;

export type AppStage = (typeof APP_STAGES)[number];

/** Human label for a stage — the wire word is snake_case, the bar is not. */
export const APP_STAGE_LABELS: Record<AppStage, string> = {
  scoped: "Scoped",
  scaffolded: "Scaffolded",
  files_written: "Files written",
  deployed: "Deployed",
  live: "Live",
};

export type AppVisibility = "private" | "shared" | "public";
export type AppStatus = "draft" | "live" | "archived";

/** The live build session on an app, if there is one. `stage` is null until
    the first stage event lands (nothing publishes them in stage 1). */
export interface AppOpenSession {
  id: number;
  channel_id: number;
  stage: AppStage | null;
  locked_until: string;
}

/** AppOut. `id` is a random 12-char base32 string, never a sequence (D4). */
export interface App {
  id: string;
  name: string;
  description: string;
  visibility: AppVisibility;
  status: AppStatus;
  owner_user_id: number;
  builder_bot_id: number;
  /** Lineage — the app this one was remixed from. Recorded from day one
      because it cannot be retrofitted (Amendment A edit 3). */
  parent_app_id: string | null;
  created_at: string;
  updated_at: string;
  /** Is it on MY menu (owned, or added from discover)? */
  on_menu: boolean;
  open_session: AppOpenSession | null;
}

/**
 * BuilderOut — a resident seat offered as a builder.
 *
 * `model` is read from the seat's own config at request time and printed as
 * given (provenance-printing policy). `null` is a real answer, not an error:
 * the card says the seat does not declare one. Never substitute a model name.
 */
export interface Builder {
  bot_id: number;
  name: string;
  avatar_url?: string | null;
  model: string | null;
  builds_total: number;
  builds_live: number;
}

/** Quota unit = build sessions started per user per UTC day (D5). */
export interface Quota {
  cap: number;
  used: number;
  left: number;
  resets_at: string;
}

/** Why a turn stopped. A closed set: each value is a sentence the server
    already wrote into the room, and the chip below is its short form. */
export type HaltReason = "timeout" | "error" | "secret" | "ceiling" | "stopped";

/**
 * What a stage event says about the turn that produced it (stage 2, §1.2).
 *
 * Every key is optional and unknown keys still ride along: `detail` is
 * free-form on the wire and a stage-1 event's bare `{}` is still a valid
 * event. These are the keys this client RENDERS, typed so that a renderer
 * reaching for one that is not there has to say what it does instead.
 */
export interface StageDetail {
  /** 1-based, and the same number on every event of one turn. */
  turn?: number;
  /** Paths the turn's commit touched. Capped by the publisher at 40 names
      plus a literal `"+N more"` entry, which is plain text, not a path. */
  files?: string[];
  tokens?: number;
  model?: string;
  /** The turn ended clean with an empty diff: an answer, a refusal, or a
      decision that nothing needed changing. It ENDS the turn. */
  no_changes?: boolean;
  /** The runner's one-line report. Plain text, always — never markup. */
  summary?: string;
  halted?: HaltReason;
  reason?: string;
  [key: string]: unknown;
}

/** StageEventOut. `detail` is a bounded JSON object (filenames and the like). */
export interface StageEvent {
  id: number;
  session_id: number;
  stage: AppStage;
  detail: StageDetail;
  created_at: string;
}

/**
 * The latest turn on a session, derived from its stage events.
 *
 * Derived rather than stored: the server sends events, not a turn record, and
 * a client that kept its own turn row would have two answers the moment a
 * reload replayed `stages`. `onStage` and the reload path both build this the
 * same way, from the same input.
 */
export interface TurnState {
  turn: number;
  files: string[];
  halted: HaltReason | null;
  no_changes: boolean;
  summary: string | null;
  /** True when the turn stopped WAITING on anything: it halted, or it ended
      clean with an empty diff. The elapsed clock stops here — a turn that
      ends must end the bar's wait, not leave the user watching a counter
      (spec §A). A turn that wrote files does NOT set it: the session goes on,
      and the clock is the session's, not the turn's. */
  done: boolean;
}

/** SessionOut — everything the build modal renders, in one payload. */
export interface AppSession {
  id: number;
  app: App;
  channel_id: number;
  builder: Builder;
  started_at: string;
  locked_until: string;
  ended_at: string | null;
  stage: AppStage | null;
  stages: StageEvent[];
  quota: Quota;
  /** Client-side, derived from `stages` — not a field the server sends. Null
      until the first event carrying a turn arrives. */
  lastTurn?: TurnState | null;
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

/**
 * A text channel was deleted, for good — it and every message in it.
 *
 * Sent to everyone who could see the channel (public: everyone; private: its
 * members), the deleter's own tab included, so the frame is the one path that
 * has to be idempotent with the DELETE the deleter just made. `channel` is
 * the row as it was a moment ago: enough to name it in a notice after the
 * sidebar entry is gone.
 */
export interface ChannelDeleteFrame {
  type: "channel_delete";
  channel_id: number;
  /** Who deleted it — its owner, or an admin. Optional for the same reason
      the member frames' is: never assume it resolves. */
  by_user_id?: number | null;
  channel: {
    id: number;
    type: ChannelType;
    name: string;
    visibility?: ChannelVisibility;
  };
}

export interface MemberAddFrame extends MemberEventFrame {
  type: "member_add";
}

export interface MemberRemoveFrame extends MemberEventFrame {
  type: "member_remove";
}

/**
 * A build reached a stage. Fanned out to EVERY socket of the session's owner
 * and to nobody else — not the builder bot, not other users (brief D7).
 *
 * The frame carries no row id (the persisted event has one; the frame does
 * not), so the store synthesizes a local id for the events it appends live.
 */
export interface AppStageFrame {
  type: "app_stage";
  session_id: number;
  app_id: string;
  stage: AppStage;
  detail: StageDetail;
  created_at: string;
}

/** An app row changed (rename, status). Owner's sockets only. */
export interface AppUpdateFrame {
  type: "app_update";
  app: App;
}

export type ServerFrame =
  | ReadyFrame
  | MessageCreateFrame
  | MessageEditFrame
  | MessageDeleteFrame
  | TypingStartFrame
  | PresenceFrame
  | ChannelCreateFrame
  | ChannelDeleteFrame
  | MemberAddFrame
  | MemberRemoveFrame
  | AppStageFrame
  | AppUpdateFrame;

/* ---- Web Push payload (WP7 shape; consumed by src/sw.ts) ---- */

export interface PushPayload {
  title: string;
  body: string;
  channel_id: number;
  message_id: number;
  url: string; // e.g. "/channels/3"
}

/* ---- plan room (SPECS/2026-08-20-plan-room.md) ---- */

/* Ruled #custodian seq 1391 item 1. Left-to-right, and the order is
   load-bearing: it is the board. */
export const PLAN_COLUMNS = [
  "Backlog",
  "Proposed",
  "Ready",
  "Building",
  "Review",
  "Merged",
  "Archived",
] as const;

export type PlanColumn = (typeof PLAN_COLUMNS)[number];

/* green = prod matches the mirror; amber = merged, not deployed; red = LIVE,
   NOT MERGED — the dangerous one, meaning code is running that the mirror has
   never seen. Computed once, broker-side, by metrics.deploy_state(). */
export type DeployBadge = "green" | "amber" | "red" | "unknown";

export interface PlanDeploy {
  badge: DeployBadge;
  detail: string;
  state?: string;
  ahead?: number;
  behind?: number;
}

/* The board's own staleness, said out loud. The board cannot go stale relative
   to the mirror — it is not a copy of it — but the mirror can lag, so every
   render says which mirror head it derived from and when. Declared, never
   denied. */
export interface PlanFace {
  available: boolean;
  unavailable_reason?: string;
  derived_at?: string;
  mirror_head?: string | null;
  mirror?: string;
  deploy?: PlanDeploy;
  gate_configured?: boolean;
  notes?: string[];
  columns?: string[];
  column_blurbs?: Record<string, string>;
}

/* A card is a RENDERING of an artifact that already exists. The derived half
   comes from the broker-written index; the four board-native fields (blocked,
   blocked_reason, archived, sort_order) plus comment_count are the server's,
   and are the only things anything in this client can change. */
export interface PlanCard {
  slug: string;
  kind: "spec" | "backlog" | "keyboard";
  title: string;
  column: string;
  spec_path: string | null;
  status: string | null;
  status_word: string | null;
  tier: string | null;
  tier_note?: string | null;
  lane: string | null;
  review_owner: string | null;
  builder: string | null;
  requester?: string | null;
  cross_lane?: boolean;
  confirm_seq: number | null;
  branch?: string | null;
  shas?: string[];
  flags: string[];
  deploy: PlanDeploy | null;
  whose_move: "plink" | "residents" | "nobody";
  opened_at: string | null;
  updated_at: string | null;
  note: string;
  where: string;
  merge_commit?: string | null;
  shortstat?: string;
  guarded_paths?: string[];
  body?: string;
  position?: number;
  /* board-native — the complete list of what the board owns */
  blocked: boolean;
  blocked_reason: string | null;
  blocked_by: string | null;
  blocked_at: string | null;
  archived: boolean;
  sort_order: number | null;
  comment_count: number;
}

/* A backlog row — the artifact a `kind: "backlog"` card renders. The four
   settable statuses are the four triage verbs; `open` is the filed state and
   has no verb that returns to it. `spec_ref` is the spec SLUG, never a path. */
export type BacklogStatus =
  | "open"
  | "spec'd"
  | "built"
  | "rejected"
  | "duplicate";

export interface BacklogItem {
  id: number;
  text: string;
  author: string;
  created_at: string;
  status: BacklogStatus;
  spec_ref: string | null;
  /* Who changed the status and when — typed, the same shape a message author
     has. Null on a row nobody has triaged. */
  status_by_type: MemberType | null;
  status_by_id: number | null;
  status_at: string | null;
}

export interface PlanComment {
  id: number;
  slug: string;
  author_type: MemberType;
  author_id: number;
  author_label: string;
  text: string;
  created_at: string;
}

export interface PlanBoard {
  face: PlanFace;
  cards: PlanCard[];
  counts: Record<string, number>;
}

export interface PlanCardDetail {
  card: PlanCard | null;
  comments: PlanComment[];
  face: PlanFace;
  note?: string;
}
