/* Typed REST client. All URLs are relative (dev: vite proxy -> :8000;
   prod: same origin). Cookies ride along via credentials: "include".
   Errors surface as ApiError with the server's `detail` string. */

import type {
  AvatarUploadResponse,
  BackfillItem,
  Bot,
  ChannelListItem,
  ChannelMemberOut,
  ChannelVisibility,
  DmResponse,
  Message,
  NotifyPrefs,
  PickerItem,
  PlanBoard,
  PlanCard,
  PlanCardDetail,
  SearchResult,
  SettableStatus,
  SummarizeResponse,
  UnfurlData,
  UploadResponse,
  User,
} from "./types";

/**
 * The exact `detail` the server sends on a rotation-gate 403. It is API
 * surface on purpose — server/app/routers/auth.py calls it out as such — and
 * matching it is how the client learns the flag at all: GET /me returns the
 * public User shape, which deliberately does not carry
 * `must_change_password`.
 */
export const PASSWORD_CHANGE_REQUIRED = "Password change required";

/** Mirrors PASSWORD_MIN_LENGTH in server/app/routers/auth.py. */
export const PASSWORD_MIN_LENGTH = 12;

/**
 * Called whenever any request comes back walled off by the rotation gate.
 *
 * Registered by the session store rather than imported from it: the store
 * already imports this module, and importing it back would be a cycle. This
 * keeps the dependency pointing one way and means a 403 on ANY call — not
 * just the ones someone remembered to special-case — routes the user to the
 * change form.
 */
type RotationHandler = () => void;
let onRotationRequired: RotationHandler = () => {};
export function setRotationHandler(fn: RotationHandler): void {
  onRotationRequired = fn;
}

export class ApiError extends Error {
  readonly status: number;
  /**
   * Server-provided `detail`, or a generic fallback. Every error body the
   * server emits — including 422 validation failures, which the server now
   * flattens to one "Invalid request: …" sentence rather than a list — puts a
   * plain string here, so callers can show it verbatim instead of
   * second-guessing the status. It is server-authored text: render it as
   * text, never as markup.
   */
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      credentials: "include",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Network error — server unreachable");
  }
  if (!res.ok) {
    let detail = res.statusText || "Request failed";
    try {
      const data: unknown = await res.json();
      if (
        typeof data === "object" &&
        data !== null &&
        "detail" in data &&
        typeof (data as { detail: unknown }).detail === "string"
      ) {
        detail = (data as { detail: string }).detail;
      }
    } catch {
      /* non-JSON error body — keep statusText */
    }
    if (res.status === 403 && detail === PASSWORD_CHANGE_REQUIRED) {
      // Every route except the three exempt ones answers like this until the
      // password is rotated, so this is the one place that needs to notice.
      onRotationRequired();
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/* ---- auth ---- */

/**
 * Change your own password. The server also ends every OTHER session for this
 * user — that eviction is the point of the feature, not a side effect — and
 * keeps the calling one, so the tab stays logged in.
 */
export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("POST", "/auth/password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}

export function login(username: string, password: string): Promise<User> {
  return request<User>("POST", "/auth/login", { username, password });
}

export function logout(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("POST", "/auth/logout");
}

export function fetchMe(): Promise<User> {
  return request<User>("GET", "/me");
}

export function updateMe(patch: {
  display_name?: string;
  status?: SettableStatus;
}): Promise<User> {
  return request<User>("PATCH", "/me", patch);
}

/* ---- channels ---- */

export function listChannels(): Promise<ChannelListItem[]> {
  return request<ChannelListItem[]>("GET", "/channels");
}

/** Create a named text channel (name: lowercase a-z, 0-9, dashes; 1-32 chars).
    409 -> name taken, 400 -> invalid name (both surface as ApiError.detail).

    A `private` channel starts with exactly one member — you, its owner — and
    only you can invite anyone else into it. */
export function createChannel(
  name: string,
  visibility: ChannelVisibility = "public",
): Promise<ChannelListItem> {
  return request<ChannelListItem>("POST", "/channels", { name, visibility });
}

/**
 * Add a user to a private channel. OWNER ONLY server-side (403 otherwise) —
 * this client only offers the affordance to the owner, but the refusal, not
 * the hidden button, is the wall. Idempotent: `added: false` = already in.
 */
export function inviteToChannel(
  channelId: number,
  userId: number,
): Promise<{ ok: boolean; added: boolean }> {
  return request("POST", `/channels/${channelId}/invite`, { user_id: userId });
}

/** Remove a user from a private channel. Owner only; 400 if the target IS the
    owner (an owner's only way out is leaving). */
export function kickFromChannel(
  channelId: number,
  userId: number,
): Promise<{ ok: boolean; removed: boolean }> {
  return request("POST", `/channels/${channelId}/kick`, { user_id: userId });
}

/** Leave a private channel — anyone may, the owner included (who keeps
    ownership, and with it the power to re-add themselves). Idempotent. */
export function leaveChannel(
  channelId: number,
): Promise<{ ok: boolean; left: boolean }> {
  return request("POST", `/channels/${channelId}/leave`);
}

/**
 * Delete a text channel and everything posted in it. Its OWNER or an admin
 * (403 otherwise); text channels only — the main feed and DMs are not
 * deletable and answer 400, which is why this client never offers the
 * affordance there. Everyone who could see the channel gets a
 * `channel_delete` frame, the caller included.
 */
export function deleteChannel(channelId: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("DELETE", `/channels/${channelId}`);
}

export function openDm(userId: number): Promise<DmResponse> {
  return request<DmResponse>("POST", "/dms", { user_id: userId });
}

export function markRead(
  channelId: number,
  seq: number,
): Promise<{ channel_id: number; last_read_seq: number }> {
  return request("PUT", `/channels/${channelId}/read`, { seq });
}

export function listMembers(channelId: number): Promise<ChannelMemberOut[]> {
  return request<ChannelMemberOut[]>("GET", `/channels/${channelId}/members`);
}

/* ---- bots ---- */

/** Every bot on the server (public shape), for the "add a bot" picker. */
export function listBots(): Promise<Bot[]> {
  return request<Bot[]>("GET", "/bots");
}

/**
 * Add a bot to a channel. Participant-gated server-side — a DM only accepts
 * this from one of its two members, and that gate, not this call site, is the
 * privacy wall. 403 surfaces as ApiError.detail.
 */
export function addChannelBot(
  channelId: number,
  botId: number,
): Promise<{ ok: boolean; added: boolean }> {
  return request("POST", `/channels/${channelId}/bots`, { bot_id: botId });
}

export function removeChannelBot(
  channelId: number,
  botId: number,
): Promise<{ ok: boolean; removed: boolean }> {
  return request("DELETE", `/channels/${channelId}/bots/${botId}`);
}

/* ---- messages ---- */

export function sendMessage(
  channelId: number,
  content: string,
  opts: { reply_to_id?: number } = {},
): Promise<Message> {
  return request<Message>("POST", `/channels/${channelId}/messages`, {
    content,
    ...opts,
  });
}

export function editMessage(messageId: number, content: string): Promise<Message> {
  return request<Message>("PATCH", `/messages/${messageId}`, { content });
}

export function deleteMessage(messageId: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("DELETE", `/messages/${messageId}`);
}

/** Scrollback: newest-first; deleted messages omitted. */
export function fetchHistory(
  channelId: number,
  opts: { beforeSeq?: number; limit?: number } = {},
): Promise<Message[]> {
  const params = new URLSearchParams();
  if (opts.beforeSeq !== undefined) params.set("before_seq", String(opts.beforeSeq));
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return request<Message[]>(
    "GET",
    `/channels/${channelId}/messages${qs ? `?${qs}` : ""}`,
  );
}

/** Backfill: ascending from `fromSeq`, current-state, tombstones included. */
export function fetchBackfill(
  channelId: number,
  fromSeq: number,
  limit = 200,
): Promise<BackfillItem[]> {
  return request<BackfillItem[]>(
    "GET",
    `/channels/${channelId}/messages?from_seq=${fromSeq}&limit=${limit}`,
  );
}

export function search(q: string): Promise<SearchResult[]> {
  return request<SearchResult[]>("GET", `/search?q=${encodeURIComponent(q)}`);
}

/**
 * The one message a seq names in a channel, shaped like a search result so
 * the search panel can show it and `goTo` can jump to it — or null.
 *
 * Reuses the backfill read (`from_seq` + `limit=1`), which returns the first
 * message with seq >= N in ascending order; a match is only a match if the
 * seq is exactly N. No new endpoint: this is the same row a resident's
 * read_message reads, seen from the human side.
 */
export async function messageBySeq(
  channel: ChannelListItem,
  seq: number,
): Promise<SearchResult | null> {
  const rows = await request<Message[]>(
    "GET",
    `/channels/${channel.id}/messages?from_seq=${seq}&limit=1`,
  );
  const hit = rows.find((m) => m.seq === seq);
  if (hit === undefined) return null;
  return {
    message: hit,
    channel: { id: channel.id, type: channel.type, name: channel.name },
  };
}

/* ---- voice-to-text (WP12) ---- */

/**
 * POST /stt (multipart field `audio`) -> transcribed text.
 * Throws ApiError(501) when no STT engine is installed server-side.
 */
export async function transcribeAudio(
  blob: Blob,
  filename: string,
): Promise<{ text: string }> {
  const form = new FormData();
  form.append("audio", blob, filename);
  let res: Response;
  try {
    res = await fetch("/stt", {
      method: "POST",
      credentials: "include",
      body: form,
    });
  } catch {
    throw new ApiError(0, "Network error — transcription failed");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(
      res.status,
      detailFromBody(text, res.statusText || "Transcription failed"),
    );
  }
  return (await res.json()) as { text: string };
}

/* ---- media (WP10) ---- */

function detailFromBody(text: string, fallback: string): string {
  try {
    const data: unknown = JSON.parse(text);
    if (
      typeof data === "object" &&
      data !== null &&
      "detail" in data &&
      typeof (data as { detail: unknown }).detail === "string"
    ) {
      return (data as { detail: string }).detail;
    }
  } catch {
    /* non-JSON body */
  }
  return fallback;
}

/**
 * Upload files as STAGED attachments (message_id NULL). XHR (not fetch) so we
 * get real upload progress events. Link to a message later via
 * claimAttachments — see server/app/routers/media.py docstring, flow 1.
 */
export function uploadFiles(
  files: File[],
  onProgress?: (fraction: number) => void,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/upload");
    xhr.withCredentials = true;
    xhr.responseType = "text";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress !== undefined) {
        onProgress(e.total > 0 ? e.loaded / e.total : 0);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.response as string) as UploadResponse);
        } catch {
          reject(new ApiError(xhr.status, "Malformed upload response"));
        }
      } else {
        reject(
          new ApiError(
            xhr.status,
            detailFromBody(xhr.response as string, "Upload failed"),
          ),
        );
      }
    };
    xhr.onerror = () =>
      reject(new ApiError(0, "Network error — upload failed"));
    xhr.send(form);
  });
}

/** Link staged uploads to a message you authored; server publishes message_edit. */
export function claimAttachments(
  attachmentIds: number[],
  messageId: number,
): Promise<Message> {
  return request<Message>("POST", "/attachments/claim", {
    attachment_ids: attachmentIds,
    message_id: messageId,
  });
}

export function fetchPicker(tab: "gif" | "image"): Promise<PickerItem[]> {
  return request<PickerItem[]>("GET", `/picker?tab=${tab}`);
}

/**
 * Add one image to a picker tab. Multipart, so it bypasses request()'s JSON
 * body handling. The server derives the stored extension from the decoded
 * image format and resolves name collisions, so the returned item's `name`
 * may differ from the file you handed in — always append what comes back.
 */
export async function addPickerItem(
  tab: "gif" | "image",
  file: File,
): Promise<PickerItem> {
  const form = new FormData();
  form.append("file", file);
  form.append("tab", tab);
  let res: Response;
  try {
    res = await fetch("/picker/add", {
      method: "POST",
      credentials: "include",
      body: form,
    });
  } catch {
    throw new ApiError(0, "Network error — server unreachable");
  }
  if (!res.ok) {
    throw new ApiError(
      res.status,
      detailFromBody(await res.text(), "Could not add to picker"),
    );
  }
  return (await res.json()) as PickerItem;
}

/** Remove a picker asset. The picker is a shared shelf — this affects everyone. */
export function deletePickerItem(
  tab: "gif" | "image",
  name: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    "DELETE",
    `/picker/file/${tab}/${encodeURIComponent(name)}`,
  );
}

/* ---- unfurl / summarize ---- */

export function fetchUnfurl(url: string): Promise<UnfurlData> {
  return request<UnfurlData>("GET", `/unfurl?url=${encodeURIComponent(url)}`);
}

export function summarizeUrl(url: string): Promise<SummarizeResponse> {
  return request<SummarizeResponse>("POST", "/summarize", { url });
}

/* ---- notifications (WP11) ---- */

/** Throws ApiError(503) when push is not configured server-side. */
export function getVapidPublicKey(): Promise<{ key: string }> {
  return request<{ key: string }>("GET", "/vapid-public-key");
}

export function pushSubscribe(
  endpoint: string,
  keys: Record<string, string>,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("POST", "/push/subscribe", { endpoint, keys });
}

export function pushUnsubscribe(
  endpoint: string,
): Promise<{ ok: boolean; removed: boolean }> {
  return request<{ ok: boolean; removed: boolean }>(
    "DELETE",
    "/push/subscribe",
    { endpoint },
  );
}

export function getNotifyPrefs(): Promise<NotifyPrefs> {
  return request<NotifyPrefs>("GET", "/notify-prefs");
}

export function putNotifyPrefs(prefs: NotifyPrefs): Promise<NotifyPrefs> {
  return request<NotifyPrefs>("PUT", "/notify-prefs", prefs);
}

/* ---- avatars ---- */

/* There is deliberately no avatarUrl()/botAvatarUrl() builder here. Every
   payload that renders a face now carries `avatar_url` — the server's own
   versioned URL, keyed on the avatar file's mtime (server media.py
   avatar_version) — so the client neither guesses the endpoint nor owns a
   cache-buster. The session counter this replaced only advanced when the
   VIEWER changed their own avatar, which left a bot repainted through the
   admin surface showing its old face until the 300s max-age expired. A null
   `avatar_url` is the "no avatar, don't ask" signal; see components/Avatar. */

/* ---- plan room (SPECS/2026-08-20-plan-room.md) ---- */

/** GET /planroom/board. Every card, in column order, plus the board's face.
 *
 * The face carries when the board was derived and from which mirror head, and
 * the view always shows both — the board cannot go stale relative to the mirror
 * because it is not a copy of it, but the mirror itself can lag, and staleness
 * in this house is declared rather than denied. `face.available === false` is
 * not an error: it means the derived index is missing or unreadable, which must
 * not read like an empty board. */
export function planBoard(filters?: {
  column?: string;
  lane?: string;
  owner?: string;
  blocked?: boolean;
}): Promise<PlanBoard> {
  const qs = new URLSearchParams();
  if (filters?.column !== undefined) qs.set("column", filters.column);
  if (filters?.lane !== undefined) qs.set("lane", filters.lane);
  if (filters?.owner !== undefined) qs.set("owner", filters.owner);
  if (filters?.blocked !== undefined) qs.set("blocked", String(filters.blocked));
  const q = qs.toString();
  return request<PlanBoard>("GET", `/planroom/board${q ? `?${q}` : ""}`);
}

/** GET /planroom/cards/{slug} — the card, everything on it, comments included. */
export function planCard(slug: string): Promise<PlanCardDetail> {
  return request<PlanCardDetail>(
    "GET",
    `/planroom/cards/${encodeURIComponent(slug)}`,
  );
}

/** POST a comment. ADMIN OR BOT server-side (403 otherwise) — this client only
    offers the affordance to an admin, but the refusal, not the hidden control,
    is the wall. */
export function planComment(
  slug: string,
  text: string,
): Promise<{ comment: unknown }> {
  return request("POST", `/planroom/cards/${encodeURIComponent(slug)}/comment`, {
    text,
  });
}

/** Block or unblock a card. Blocked is a FLAG WITH A REASON, NEVER A COLUMN:
    the card does not move, so everyone can see where it re-enters. A reason is
    required to block — the server refuses without one. */
export function planFlag(
  slug: string,
  blocked: boolean,
  reason?: string,
): Promise<{ card: PlanCard }> {
  return request("POST", `/planroom/cards/${encodeURIComponent(slug)}/flag`, {
    blocked,
    reason: reason ?? null,
  });
}

/** Archive a merged card. ADMIN ONLY in Phase I. */
export function planArchive(
  slug: string,
  archived: boolean,
): Promise<{ card: PlanCard }> {
  return request("POST", `/planroom/cards/${encodeURIComponent(slug)}/archive`, {
    archived,
  });
}

/** Set a card's position WITHIN its column. ADMIN ONLY in Phase I.
 *
 * This is not drag-to-column and cannot become it by accident: there is no
 * endpoint that takes a column. A card changes columns only because reality
 * moved. `null` hands the card back to the derived whose-move-first order. */
export function planOrder(
  slug: string,
  sortOrder: number | null,
): Promise<{ card: PlanCard }> {
  return request("POST", `/planroom/cards/${encodeURIComponent(slug)}/order`, {
    sort_order: sortOrder,
  });
}

/** POST /me/avatar (multipart). Server converts to 256px WebP. The response's
    `url` is the newly versioned avatar_url — put it on the session user. */
export async function uploadAvatar(file: File): Promise<AvatarUploadResponse> {
  const form = new FormData();
  form.append("file", file);
  let res: Response;
  try {
    res = await fetch("/me/avatar", {
      method: "POST",
      credentials: "include",
      body: form,
    });
  } catch {
    throw new ApiError(0, "Network error — upload failed");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(
      res.status,
      detailFromBody(text, res.statusText || "Avatar upload failed"),
    );
  }
  return (await res.json()) as AvatarUploadResponse;
}
