/* Message composer (WP10): auto-growing textarea (~8 lines max), Enter=send /
   Shift+Enter=newline (coarse pointers: Enter=newline, send button does the
   sending), throttled typing emit, reply strip, edit mode (ArrowUp in an
   empty composer edits your last message; Esc cancels), staged attachment
   uploads with thumbnail previews + progress, tabbed gif|image picker
   popover, and a reserved slot for WP12's mic button.

   Upload flow (media.py docstring, flow 1): files upload IMMEDIATELY as
   staged attachments (progress while you type), send creates the message,
   then POST /attachments/claim links them — the server publishes a
   message_edit so attachments appear via the normal WS path. Removed-before-
   send uploads are harmless orphans by design. */

import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  ChangeEvent,
  ClipboardEvent as ReactClipboardEvent,
  DragEvent as ReactDragEvent,
  KeyboardEvent as ReactKeyboardEvent,
} from "react";

import {
  ApiError,
  addPickerItem,
  claimAttachments,
  deletePickerItem,
  editMessage,
  fetchPicker,
  sendMessage,
  uploadFiles,
} from "../api";
import {
  acceptMention,
  activeMention,
  mentionCandidates,
  mentionKey,
  rankMentions,
  trackMention,
} from "../lib/mentionComplete";
import type { MentionCandidate, MentionState } from "../lib/mentionComplete";
import { pastedImages } from "../lib/pasteAttachments";
import { stripEmotionTags } from "./Markdown";
import { MentionPopup, mentionOptionId } from "./MentionPopup";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { useMessages } from "../stores/messages";
import { useSession } from "../stores/session";
import type { Message, PickerItem } from "../types";
import { socket } from "../ws";
import { MicButton } from "./MicButton";

const MAX_TEXTAREA_PX = 8 * 22 + 20; // ~8 lines + padding
const TYPING_THROTTLE_MS = 2500;

/* Mirrors messages.MAX_MESSAGE_CHARS server-side, where it is a pydantic
   max_length. Kept client-side purely as UX: the counter warns before the
   paste is sent and the pre-send guard names the limit without a round-trip.
   It is NOT the error path — a 422 that does come back carries the server's
   own flat `detail` ("Invalid request: body.content must be at most 16000
   characters"), and that string is what gets shown, because a 422 here can
   also mean something other than length. */
const MAX_MESSAGE_CHARS = 16000;
/** Show the counter only once it's about to matter. */
const COUNTER_FROM = MAX_MESSAGE_CHARS - 500;

const isCoarsePointer =
  typeof window !== "undefined" &&
  window.matchMedia("(pointer: coarse)").matches;

/* ------------------------------------------------------------- uploads */

interface PendingUpload {
  key: string;
  name: string;
  previewUrl: string | null; // object URL for images
  status: "uploading" | "done" | "error";
  progress: number; // 0..1
  attachmentId: number | null;
  errorDetail: string | null;
  /** Resolves to the staged attachment id, or null on failure. */
  promise: Promise<number | null>;
}

let uploadCounter = 0;

/* -------------------------------------------------------------- picker */

/**
 * Filename match for the picker search box.
 *
 * The name IS the only metadata a picker item has, so search is a filename
 * match. Punctuation is flattened to spaces on both sides and every query
 * token must appear, so "iron chef" finds `iron-chef-allez-cuisine-4078.gif`
 * and adding a token narrows rather than widens. The extension is stripped
 * first, otherwise typing "gif" would match the entire tab.
 *
 * This leans on files being named for their content — which download names
 * from the GIF sites mostly are, junk numeric suffix aside. Rename on the way
 * in if you want something findable.
 */
function matchesQuery(name: string, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (q === "") return true;
  const hay = name
    .replace(/\.[^.]+$/, "")
    .replace(/[^a-z0-9]+/gi, " ")
    .toLowerCase();
  return q.split(/\s+/).every((token) => hay.includes(token));
}

function PickerPopover({
  onPick,
  onClose,
}: {
  onPick: (url: string) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"gif" | "image">("gif");
  const [items, setItems] = useState<Partial<Record<"gif" | "image", PickerItem[]>>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [query, setQuery] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (items[tab] !== undefined) return;
    let alive = true;
    setError(null);
    fetchPicker(tab).then(
      (list) => {
        if (alive) setItems((s) => ({ ...s, [tab]: list }));
      },
      (err: unknown) => {
        if (alive) {
          setError(err instanceof ApiError ? err.detail : "Failed to load");
        }
      },
    );
    return () => {
      alive = false;
    };
  }, [tab, items]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const current = items[tab];
  const visible = (current ?? []).filter((item) => matchesQuery(item.name, query));

  /* Adds are sequential rather than Promise.all'd: the server resolves name
     collisions by probing the directory, so concurrent adds of same-named
     files could race onto one path. Picker adds are a handful of small files,
     so the serial cost is invisible. */
  const addFiles = async (files: File[]) => {
    if (files.length === 0 || busy) return;
    setBusy(true);
    setError(null);
    const added: PickerItem[] = [];
    let failure: string | null = null;
    for (const file of files) {
      try {
        added.push(await addPickerItem(tab, file));
      } catch (err) {
        failure = err instanceof ApiError ? err.detail : "Could not add to picker";
      }
    }
    if (added.length > 0) {
      // Server lists sorted by name; keep the open grid in that same order.
      setItems((s) => ({
        ...s,
        [tab]: [...(s[tab] ?? []), ...added].sort((a, b) =>
          a.name.localeCompare(b.name),
        ),
      }));
      // An active filter would otherwise hide what was just added, which reads
      // as the add having silently failed.
      setQuery("");
    }
    setError(failure);
    setBusy(false);
  };

  const removeItem = async (item: PickerItem) => {
    if (!window.confirm(`Remove ${item.name} from the picker for everyone?`)) return;
    try {
      await deletePickerItem(tab, item.name);
      setItems((s) => ({
        ...s,
        [tab]: (s[tab] ?? []).filter((i) => i.name !== item.name),
      }));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not remove");
    }
  };

  const accept =
    tab === "gif" ? "image/gif" : "image/png,image/jpeg,image/webp,image/gif";

  return (
    <>
      <div className="picker-scrim" onClick={onClose} />
      <div
        className={`picker-popover${dragging ? " dropping" : ""}`}
        role="dialog"
        aria-label="Image picker"
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void addFiles(Array.from(e.dataTransfer.files));
        }}
      >
        <div className="picker-tabs">
          {(["gif", "image"] as const).map((t) => (
            <button
              key={t}
              className={`picker-tab${tab === t ? " active" : ""}`}
              onClick={() => setTab(t)}
            >
              {t === "gif" ? "GIFs" : "Images"}
            </button>
          ))}
          <button
            className="picker-add"
            title={`Add to ${tab === "gif" ? "GIFs" : "Images"}`}
            aria-label={`Add to ${tab === "gif" ? "GIFs" : "Images"}`}
            disabled={busy}
            onClick={() => fileRef.current?.click()}
          >
            {busy ? "…" : "＋"}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept={accept}
            multiple
            hidden
            onChange={(e) => {
              void addFiles(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
        </div>
        <input
          className="picker-search"
          type="search"
          value={query}
          placeholder="Search by name…"
          aria-label="Search picker"
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="picker-grid">
          {error !== null && <p className="form-error">{error}</p>}
          {error === null && current === undefined && (
            <p className="picker-empty">Loading…</p>
          )}
          {current !== undefined && current.length === 0 && (
            <p className="picker-empty">Nothing here yet — drop a file in, or hit ＋.</p>
          )}
          {current !== undefined && current.length > 0 && visible.length === 0 && (
            <p className="picker-empty">No matches for “{query.trim()}”.</p>
          )}
          {visible.map((item) => (
            <div key={item.name} className="picker-item">
              <button
                className="picker-item-pick"
                title={item.name}
                onClick={() => onPick(item.url)}
              >
                <img src={item.url} alt={item.name} loading="lazy" />
              </button>
              <button
                className="picker-item-remove"
                title={`Remove ${item.name}`}
                aria-label={`Remove ${item.name}`}
                onClick={() => void removeItem(item)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------ composer */

export interface ComposerProps {
  channelId: number;
  channelName: string;
  replyTo: Message | null;
  onCancelReply: () => void;
  editing: Message | null;
  onStartEdit: (m: Message) => void;
  onCancelEdit: () => void;
  /** A message of MINE just landed in the room. The build modal freezes its
      preview on this (stage 3, Round 5: nothing changes under the mouse) —
      it is the create, not the keystroke, so the seq it carries is real. */
  onSent?: (message: Message) => void;
}

export function Composer({
  channelId,
  channelName,
  replyTo,
  onCancelReply,
  editing,
  onStartEdit,
  onCancelEdit,
  onSent,
}: ComposerProps) {
  const me = useSession((s) => s.user);
  const [value, setValue] = useState("");
  const [uploads, setUploads] = useState<PendingUpload[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [dropping, setDropping] = useState(false);
  const [mention, setMention] = useState<MentionState | null>(null);
  const members = useMembers((s) => s.byChannel[channelId]);
  const channelType = useChannels(
    (s) => s.channels.find((c) => c.id === channelId)?.type,
  );
  const mentionListId = useId();

  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const uploadsRef = useRef<PendingUpload[]>([]);
  const lastTypingRef = useRef(0);
  const draftBeforeEditRef = useRef("");
  const typedRef = useRef("");
  const caretAfterRenderRef = useRef<number | null>(null);
  const mentionPressRef = useRef(false);

  uploadsRef.current = uploads;

  const selfId = me?.id ?? null;
  const isEdit = editing !== null;
  const candidates = useMemo(
    () =>
      members === undefined
        ? []
        : mentionCandidates(members, {
            selfUserId: selfId,
            everyMessage: channelType === "app_build",
            editing: isEdit,
          }),
    [members, selfId, channelType, isEdit],
  );
  const matches =
    mention === null || mention.off ? [] : rankMentions(candidates, mention.token.query);
  const mentionOpen = matches.length > 0;
  const mentionIndex = mention === null ? 0 : Math.min(mention.index, matches.length - 1);
  // A popup that closed under a held pointer never saw the pointer come up.
  if (!mentionOpen) mentionPressRef.current = false;

  // Auto-grow.
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (ta === null) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, MAX_TEXTAREA_PX)}px`;
    // Text that was set rather than typed leaves the token's offsets stale.
    if (value !== typedRef.current) setMention(null);
  }, [value]);

  // No deps: the accepted text must be in the textarea before a caret can sit in it.
  useLayoutEffect(() => {
    const caret = caretAfterRenderRef.current;
    if (caret === null) return;
    caretAfterRenderRef.current = null;
    taRef.current?.setSelectionRange(caret, caret);
  });

  const tracking = mention !== null;
  useEffect(() => {
    if (tracking) void useMembers.getState().ensureLoaded(channelId);
  }, [tracking, channelId]);

  // Entering/leaving edit mode swaps the textarea content (draft preserved).
  useEffect(() => {
    const ta = taRef.current;
    if (editing !== null) {
      draftBeforeEditRef.current = value;
      setValue(editing.content);
      ta?.focus();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing?.id]);

  // Channel switch: clear composer-local state (parent clears reply/edit).
  useEffect(() => {
    setValue("");
    setError(null);
    setPickerOpen(false);
    setUploads((prev) => {
      for (const u of prev) {
        if (u.previewUrl !== null) URL.revokeObjectURL(u.previewUrl);
      }
      return [];
    });
  }, [channelId]);

  useEffect(() => {
    if (replyTo !== null) taRef.current?.focus();
  }, [replyTo]);

  const cancelEdit = () => {
    onCancelEdit();
    setValue(draftBeforeEditRef.current);
    draftBeforeEditRef.current = "";
  };

  /* ---- typing emit (skip while editing) ---- */

  const emitTyping = () => {
    if (editing !== null) return;
    const now = Date.now();
    if (now - lastTypingRef.current >= TYPING_THROTTLE_MS) {
      lastTypingRef.current = now;
      socket.sendTyping(channelId);
    }
  };

  /* ---- attachments ---- */

  const startUpload = (file: File) => {
    const key = `u${uploadCounter++}`;
    const isImage = file.type.startsWith("image/");
    const previewUrl = isImage ? URL.createObjectURL(file) : null;
    const patch = (partial: Partial<PendingUpload>) => {
      setUploads((prev) =>
        prev.map((u) => (u.key === key ? { ...u, ...partial } : u)),
      );
    };
    const promise = uploadFiles([file], (fraction) =>
      patch({ progress: fraction }),
    ).then(
      (res) => {
        const att = res.attachments[0];
        if (att !== undefined) {
          patch({ status: "done", progress: 1, attachmentId: att.id });
          return att.id;
        }
        patch({ status: "error", errorDetail: "Empty upload response" });
        return null;
      },
      (err: unknown) => {
        patch({
          status: "error",
          errorDetail: err instanceof ApiError ? err.detail : "Upload failed",
        });
        return null;
      },
    );
    setUploads((prev) => [
      ...prev,
      {
        key,
        name: file.name,
        previewUrl,
        status: "uploading",
        progress: 0,
        attachmentId: null,
        errorDetail: null,
        promise,
      },
    ]);
  };

  const onFilesPicked = (e: ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files !== null) {
      for (const file of Array.from(files)) startUpload(file);
    }
    e.target.value = ""; // allow re-picking the same file
  };

  const onPaste = (e: ReactClipboardEvent<HTMLTextAreaElement>) => {
    if (editing !== null) return;
    const pasted = pastedImages(
      e.clipboardData,
      new Date(),
      uploadsRef.current.map((u) => u.name),
    );
    if (pasted.length === 0) return;
    e.preventDefault();
    for (const { file, name } of pasted) {
      startUpload(
        name === file.name ? file : new File([file], name, { type: file.type }),
      );
    }
  };

  /* A drag that carries no files (selected text, a link) is left to the
     browser, and so is everything while the picker is open: it has its own
     drop target inside this one. */
  const isFileDrag = (e: ReactDragEvent<HTMLDivElement>) =>
    !pickerOpen && e.dataTransfer.types.includes("Files");

  const onDragOver = (e: ReactDragEvent<HTMLDivElement>) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    setDropping(editing === null);
  };

  const onDrop = (e: ReactDragEvent<HTMLDivElement>) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    setDropping(false);
    if (editing !== null) return;
    for (const file of Array.from(e.dataTransfer.files)) startUpload(file);
  };

  const removeUpload = (key: string) => {
    setUploads((prev) => {
      const hit = prev.find((u) => u.key === key);
      if (hit?.previewUrl != null) URL.revokeObjectURL(hit.previewUrl);
      // Staged-but-unclaimed rows are harmless orphans (server GC's them).
      return prev.filter((u) => u.key !== key);
    });
  };

  /* ---- send ---- */

  /* Pre-send only: catching an over-long paste before the round-trip lets us
     say something more useful than the server can (it does not know the draft
     is still in the box). Anything the server rejects is reported in the
     server's words. */
  const tooLongError = (n: number) =>
    `Message is ${n.toLocaleString()} characters — the limit is ${MAX_MESSAGE_CHARS.toLocaleString()}. Split it into a few messages (nothing is truncated for you).`;

  const doSend = async () => {
    if (sending) return;
    const content = value.trim();
    if (content.length > MAX_MESSAGE_CHARS) {
      setError(tooLongError(content.length));
      return;
    }

    if (editing !== null) {
      if (content.length === 0) return; // empty edit = no-op (delete is explicit)
      setSending(true);
      setError(null);
      try {
        const msg = await editMessage(editing.id, content);
        useMessages.getState().applyEdit(msg);
        onCancelEdit();
        setValue(draftBeforeEditRef.current);
        draftBeforeEditRef.current = "";
      } catch (err) {
        setError(err instanceof ApiError ? err.detail : "Edit failed");
      } finally {
        setSending(false);
      }
      return;
    }

    const hasUploads = uploadsRef.current.some((u) => u.status !== "error");
    if (content.length === 0 && !hasUploads) return;

    setSending(true);
    setError(null);
    try {
      // Wait out in-flight uploads; each promise resolves to its staged
      // attachment id (null on failure — surfaced on the thumbnail itself).
      const resolved = await Promise.all(uploadsRef.current.map((u) => u.promise));
      const ids = resolved.filter((id): id is number => id !== null);
      if (content.length === 0 && ids.length === 0) {
        setError("Attachments failed to upload");
        return;
      }
      const msg = await sendMessage(
        channelId,
        content,
        replyTo !== null ? { reply_to_id: replyTo.id } : {},
      );
      useMessages.getState().applyCreate(msg);
      onSent?.(msg);
      if (ids.length > 0) {
        const updated = await claimAttachments(ids, msg.id);
        useMessages.getState().applyEdit(updated);
      }
      setValue("");
      onCancelReply();
      setUploads((prev) => {
        for (const u of prev) {
          if (u.previewUrl !== null) URL.revokeObjectURL(u.previewUrl);
        }
        return [];
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Failed to send");
    } finally {
      setSending(false);
    }
  };

  /* ---- mention autocomplete ---- */

  const followCaret = (ta: HTMLTextAreaElement, typed: boolean) => {
    const token = activeMention(ta.value, ta.selectionStart, ta.selectionEnd);
    setMention((prev) => trackMention(prev, token, typed));
  };

  const pickMention = (item: MentionCandidate) => {
    if (mention === null) return;
    const next = acceptMention(value, mention.token, item.name);
    caretAfterRenderRef.current = next.caret;
    setValue(next.text);
    setMention(null);
    taRef.current?.focus();
  };

  /* ---- keys ---- */

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    const picked = matches[mentionIndex];
    if (mention !== null && picked !== undefined) {
      const action = mentionKey({
        key: e.key,
        shiftKey: e.shiftKey,
        altKey: e.altKey,
        ctrlKey: e.ctrlKey,
        metaKey: e.metaKey,
        isComposing: e.nativeEvent.isComposing,
        keyCode: e.keyCode,
      });
      if (action === "compose") return;
      if (action !== null) {
        e.preventDefault();
        if (action === "accept") pickMention(picked);
        else if (action === "dismiss") setMention({ ...mention, off: true });
        else {
          const step = action === "down" ? 1 : matches.length - 1;
          setMention({ ...mention, index: (mentionIndex + step) % matches.length });
        }
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !isCoarsePointer) {
      e.preventDefault();
      void doSend();
      return;
    }
    if (e.key === "Escape") {
      if (editing !== null) cancelEdit();
      else if (replyTo !== null) onCancelReply();
      return;
    }
    if (e.key === "ArrowUp" && value === "" && editing === null && me !== null) {
      const list = useMessages.getState().byChannel[channelId]?.list ?? [];
      for (let i = list.length - 1; i >= 0; i--) {
        const m = list[i];
        if (m !== undefined && m.author_type === "user" && m.author_id === me.id) {
          e.preventDefault();
          onStartEdit(m);
          return;
        }
      }
    }
  };

  /* ---- voice input (WP12): insert transcript at the cursor ---- */

  const insertAtCursor = (text: string) => {
    const ta = taRef.current;
    const current = ta !== null ? ta.value : value;
    const start = ta?.selectionStart ?? current.length;
    const end = ta?.selectionEnd ?? current.length;
    const before = current.slice(0, start);
    const after = current.slice(end);
    // Space-separate against non-whitespace neighbours.
    const pre = before.length > 0 && !/\s$/.test(before) ? " " : "";
    const post = after.length > 0 && !/^\s/.test(after) ? " " : "";
    const inserted = `${pre}${text}${post}`;
    setValue(`${before}${inserted}${after}`);
    const caret = start + pre.length + text.length;
    // Restore focus + caret after React re-renders the textarea.
    requestAnimationFrame(() => {
      const el = taRef.current;
      if (el !== null) {
        el.focus();
        el.setSelectionRange(caret, caret);
      }
    });
  };

  const pickAndPost = (url: string) => {
    setPickerOpen(false);
    sendMessage(channelId, url).then(
      (msg) => useMessages.getState().applyCreate(msg),
      (err: unknown) => {
        setError(err instanceof ApiError ? err.detail : "Failed to post image");
      },
    );
  };

  const placeholder = editing !== null ? "Edit your message…" : `Message ${channelName}`;
  const contentLength = value.trim().length;
  const overLimit = contentLength > MAX_MESSAGE_CHARS;

  return (
    <div
      className={`composer-wrap${dropping ? " dropping" : ""}`}
      onDragOver={onDragOver}
      onDragLeave={() => setDropping(false)}
      onDrop={onDrop}
    >
      {error !== null && (
        <div className="composer-error">
          <span>{error}</span>
          <button className="icon-btn" aria-label="Dismiss" onClick={() => setError(null)}>
            ✕
          </button>
        </div>
      )}

      {editing !== null && (
        <div className="composer-note">
          <span>
            Editing message — <kbd>Esc</kbd> to cancel, <kbd>Enter</kbd> to save
          </span>
          <button className="icon-btn" aria-label="Cancel edit" onClick={cancelEdit}>
            ✕
          </button>
        </div>
      )}

      {replyTo !== null && editing === null && (
        <div className="composer-reply">
          <span className="composer-reply-label">
            Replying to <b>{replyTo.author.name}</b>
          </span>
          <span className="composer-reply-snippet">
            {replyTo.content.trim().length > 0
              ? stripEmotionTags(replyTo.content)
                  .replace(/\s+/g, " ")
                  .trim()
                  .slice(0, 90)
              : "(attachment)"}
          </span>
          <button className="icon-btn" aria-label="Cancel reply" onClick={onCancelReply}>
            ✕
          </button>
        </div>
      )}

      {uploads.length > 0 && (
        <div className="upload-strip">
          {uploads.map((u) => (
            <div
              key={u.key}
              className={`upload-thumb${u.status === "error" ? " error" : ""}`}
              title={u.errorDetail ?? u.name}
            >
              {u.previewUrl !== null ? (
                <img src={u.previewUrl} alt={u.name} />
              ) : (
                <span className="upload-thumb-file">📄</span>
              )}
              {u.status === "uploading" && (
                <span
                  className="upload-progress"
                  style={{ width: `${Math.round(u.progress * 100)}%` }}
                />
              )}
              {u.status === "error" && <span className="upload-failed">failed</span>}
              <button
                className="upload-remove"
                aria-label={`Remove ${u.name}`}
                onClick={() => removeUpload(u.key)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="composer">
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={onFilesPicked}
        />
        <button
          className="icon-btn composer-btn"
          title="Attach files"
          aria-label="Attach files"
          disabled={editing !== null}
          onClick={() => fileRef.current?.click()}
        >
          ＋
        </button>
        <textarea
          ref={taRef}
          className="composer-input"
          rows={1}
          placeholder={placeholder}
          value={value}
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={mentionOpen}
          aria-controls={mentionOpen ? mentionListId : undefined}
          aria-activedescendant={
            mentionOpen ? mentionOptionId(mentionListId, mentionIndex) : undefined
          }
          onChange={(e) => {
            typedRef.current = e.target.value;
            setValue(e.target.value);
            followCaret(e.target, true);
            emitTyping();
          }}
          onSelect={(e) => followCaret(e.currentTarget, false)}
          onBlur={() => {
            if (!mentionPressRef.current) setMention(null);
          }}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
        {contentLength >= COUNTER_FROM && (
          <span
            className={`composer-count${overLimit ? " over" : ""}`}
            aria-live="polite"
          >
            {(MAX_MESSAGE_CHARS - contentLength).toLocaleString()}
          </span>
        )}
        <div className="composer-btns">
          <button
            className="icon-btn composer-btn"
            title="Image & GIF picker"
            aria-label="Image and GIF picker"
            disabled={editing !== null}
            onClick={() => setPickerOpen((v) => !v)}
          >
            GIF
          </button>
          <MicButton
            disabled={editing !== null}
            onText={insertAtCursor}
            onError={setError}
          />
          <button
            className="icon-btn composer-btn composer-send"
            title={
              overLimit
                ? `Over the ${MAX_MESSAGE_CHARS.toLocaleString()}-character limit`
                : editing !== null
                  ? "Save edit"
                  : "Send"
            }
            aria-label={editing !== null ? "Save edit" : "Send message"}
            disabled={sending || overLimit}
            onClick={() => void doSend()}
          >
            ➤
          </button>
        </div>
        {pickerOpen && (
          <PickerPopover onPick={pickAndPost} onClose={() => setPickerOpen(false)} />
        )}
        {mentionOpen && (
          <MentionPopup
            id={mentionListId}
            items={matches}
            activeIndex={mentionIndex}
            onActive={(index) => setMention((m) => (m === null ? m : { ...m, index }))}
            onPick={pickMention}
            onPress={(down) => {
              mentionPressRef.current = down;
            }}
          />
        )}
      </div>
    </div>
  );
}
