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
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { ChangeEvent, KeyboardEvent as ReactKeyboardEvent } from "react";

import {
  ApiError,
  claimAttachments,
  editMessage,
  fetchPicker,
  sendMessage,
  uploadFiles,
} from "../api";
import { useMessages } from "../stores/messages";
import { useSession } from "../stores/session";
import type { Message, PickerItem } from "../types";
import { socket } from "../ws";
import { MicButton } from "./MicButton";

const MAX_TEXTAREA_PX = 8 * 22 + 20; // ~8 lines + padding
const TYPING_THROTTLE_MS = 2500;

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

  return (
    <>
      <div className="picker-scrim" onClick={onClose} />
      <div className="picker-popover" role="dialog" aria-label="Image picker">
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
        </div>
        <div className="picker-grid">
          {error !== null && <p className="form-error">{error}</p>}
          {error === null && current === undefined && (
            <p className="picker-empty">Loading…</p>
          )}
          {current !== undefined && current.length === 0 && (
            <p className="picker-empty">Nothing here yet.</p>
          )}
          {current?.map((item) => (
            <button
              key={item.name}
              className="picker-item"
              title={item.name}
              onClick={() => onPick(item.url)}
            >
              <img src={item.url} alt={item.name} loading="lazy" />
            </button>
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
}

export function Composer({
  channelId,
  channelName,
  replyTo,
  onCancelReply,
  editing,
  onStartEdit,
  onCancelEdit,
}: ComposerProps) {
  const me = useSession((s) => s.user);
  const [value, setValue] = useState("");
  const [uploads, setUploads] = useState<PendingUpload[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);

  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const uploadsRef = useRef<PendingUpload[]>([]);
  const lastTypingRef = useRef(0);
  const draftBeforeEditRef = useRef("");

  uploadsRef.current = uploads;

  // Auto-grow.
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (ta === null) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, MAX_TEXTAREA_PX)}px`;
  }, [value]);

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

  const removeUpload = (key: string) => {
    setUploads((prev) => {
      const hit = prev.find((u) => u.key === key);
      if (hit?.previewUrl != null) URL.revokeObjectURL(hit.previewUrl);
      // Staged-but-unclaimed rows are harmless orphans (server GC's them).
      return prev.filter((u) => u.key !== key);
    });
  };

  /* ---- send ---- */

  const doSend = async () => {
    if (sending) return;
    const content = value.trim();

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

  /* ---- keys ---- */

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
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

  return (
    <div className="composer-wrap">
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
              ? replyTo.content.replace(/\s+/g, " ").slice(0, 90)
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
          onChange={(e) => {
            setValue(e.target.value);
            emitTyping();
          }}
          onKeyDown={onKeyDown}
        />
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
            title={editing !== null ? "Save edit" : "Send"}
            aria-label={editing !== null ? "Save edit" : "Send message"}
            disabled={sending}
            onClick={() => void doSend()}
          >
            ➤
          </button>
        </div>
        {pickerOpen && (
          <PickerPopover onPick={pickAndPost} onClose={() => setPickerOpen(false)} />
        )}
      </div>
    </div>
  );
}
