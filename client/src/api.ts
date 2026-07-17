/* Typed REST client. All URLs are relative (dev: vite proxy -> :8000;
   prod: same origin). Cookies ride along via credentials: "include".
   Errors surface as ApiError with the server's `detail` string. */

import type {
  AvatarUploadResponse,
  BackfillItem,
  ChannelListItem,
  ChannelMemberOut,
  DmResponse,
  Message,
  NotifyPrefs,
  PickerItem,
  SearchResult,
  SettableStatus,
  SummarizeResponse,
  UnfurlData,
  UploadResponse,
  User,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  /** Server-provided `detail`, or a generic fallback. */
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
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/* ---- auth ---- */

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

/* Cache-buster bumped after an avatar upload so every <img> rendered from
   then on bypasses the browser's cached copy of /avatars/{id}. */
let avatarVersion = 0;

export function bumpAvatarVersion(): void {
  avatarVersion += 1;
}

/** Unsigned avatar URL; the server 404s when the user has none set. */
export function avatarUrl(userId: number): string {
  return avatarVersion > 0
    ? `/avatars/${userId}?v=${avatarVersion}`
    : `/avatars/${userId}`;
}

/** POST /me/avatar (multipart). Server converts to 256px WebP. */
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
