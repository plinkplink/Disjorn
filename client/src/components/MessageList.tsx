/* Chat feed (WP10): seq-ordered messages with day dividers, author grouping
   (5-minute collapse), infinite scrollback with scroll preservation, stick-to-
   bottom + "New messages" jump pill, hover actions, reply headers with
   jump-and-flash, chibi rendering on bot messages, attachment rendering, and
   unfurl cards.

   AppShell already handles ensureLoaded / mark-read / sendFocus on channel
   switch — this component only renders and paginates. */

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { useEffect } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";

import { ApiError, deleteMessage } from "../api";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { useMessages } from "../stores/messages";
import { useSession } from "../stores/session";
import type { Attachment, Message } from "../types";
import { Avatar } from "./Avatar";
import { firstHttpUrl, Markdown } from "./Markdown";
import { UnfurlCard } from "./UnfurlCard";

const GROUP_GAP_MS = 5 * 60 * 1000;

/* Touch (WP12): hover actions can't hover — a tap on the row toggles them. */
const isCoarsePointer =
  typeof window !== "undefined" &&
  window.matchMedia("(pointer: coarse)").matches;
const BOTTOM_STICK_PX = 60;
const TOP_FETCH_PX = 250;
const MARK_READ_THROTTLE_MS = 1500;
const EMPTY_LIST: Message[] = [];

/* ------------------------------------------------------------- utilities */

function ts(message: Message): number {
  return new Date(message.created_at).getTime();
}

function dayLabel(date: Date): string {
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86_400_000);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString(undefined, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function shortTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

function fullTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** emote_refs "chibi:{pack}/{Category}/{File.png}" -> served chibi URL. */
function chibiUrls(message: Message): string[] {
  const out: string[] = [];
  for (const ref of message.emote_refs) {
    if (typeof ref === "string" && ref.startsWith("chibi:")) {
      out.push(`/chibi/${ref.slice("chibi:".length)}`);
    }
  }
  return out;
}

/* ------------------------------------------------------------- feed model */

type FeedItem =
  | { kind: "divider"; key: string; label: string }
  | { kind: "message"; key: string; message: Message; withHeader: boolean };

function buildFeed(list: Message[]): FeedItem[] {
  const out: FeedItem[] = [];
  let prev: Message | null = null;
  let prevDay = "";
  for (const m of list) {
    const date = new Date(m.created_at);
    const day = date.toDateString();
    let newDay = false;
    if (day !== prevDay) {
      out.push({ kind: "divider", key: `d${day}`, label: dayLabel(date) });
      prevDay = day;
      newDay = true;
    }
    const withHeader =
      prev === null ||
      newDay ||
      prev.author_type !== m.author_type ||
      prev.author_id !== m.author_id ||
      m.reply_to_id !== null ||
      ts(m) - ts(prev) > GROUP_GAP_MS;
    out.push({ kind: "message", key: `m${m.id}`, message: m, withHeader });
    prev = m;
  }
  return out;
}

/* ---------------------------------------------------------------- pieces */

function AuthorAvatar({ message }: { message: Message }) {
  if (message.author_type === "user") {
    return <Avatar userId={message.author_id} name={message.author.name} size={38} />;
  }
  // Bots have no /avatars/{id} endpoint — letter fallback, never a wrong
  // user's avatar (bot ids can collide with user ids).
  return (
    <div className="avatar avatar-bot" style={{ width: 38, height: 38 }} aria-hidden>
      {message.author.name.slice(0, 1).toUpperCase()}
    </div>
  );
}

function ReplyRef({
  original,
  onJumpTo,
}: {
  original: Message | undefined;
  onJumpTo: (id: number) => void;
}) {
  if (original === undefined) {
    return (
      <div className="msg-reply-ref muted">
        <span className="reply-spine" aria-hidden />
        Original message not loaded
      </div>
    );
  }
  const snippet =
    original.content.trim().length > 0
      ? truncate(original.content.replace(/\s+/g, " "), 90)
      : original.attachments.length > 0
        ? "(attachment)"
        : "(empty message)";
  return (
    <button className="msg-reply-ref" onClick={() => onJumpTo(original.id)}>
      <span className="reply-spine" aria-hidden />
      <span className="reply-author">@{original.author.name}</span>
      <span className="reply-snippet">{snippet}</span>
    </button>
  );
}

function AttachmentItem({
  att,
  onOpenImage,
}: {
  att: Attachment;
  onOpenImage: (att: Attachment) => void;
}) {
  if (att.mime_type.startsWith("image/") && att.url !== null) {
    return (
      <button
        className="attachment-image"
        title={att.original_filename}
        onClick={() => onOpenImage(att)}
      >
        <img src={att.url} alt={att.original_filename} loading="lazy" />
      </button>
    );
  }
  return (
    <a
      className="attachment-file"
      href={att.url ?? undefined}
      target="_blank"
      rel="noopener noreferrer"
    >
      <span className="attachment-file-icon" aria-hidden>
        📄
      </span>
      <span className="attachment-file-name">{att.original_filename}</span>
      <span className="attachment-file-size">{formatSize(att.size_bytes)}</span>
    </a>
  );
}

interface RowProps {
  message: Message;
  withHeader: boolean;
  isMine: boolean;
  hasReplies: boolean;
  original: Message | undefined;
  mentionNames: string[];
  onReply: (m: Message) => void;
  onEdit: (m: Message) => void;
  onOpenImage: (att: Attachment) => void;
  onSummarize: (url: string) => void;
  onJumpTo: (id: number) => void;
}

function MessageRow({
  message,
  withHeader,
  isMine,
  hasReplies,
  original,
  mentionNames,
  onReply,
  onEdit,
  onOpenImage,
  onSummarize,
  onJumpTo,
}: RowProps) {
  const chibis = message.author_type === "bot" ? chibiUrls(message) : [];
  const unfurlUrl = useMemo(
    () => firstHttpUrl(message.content),
    [message.content],
  );
  const hasText = message.content.trim().length > 0;
  // Touch: no hover — tapping the row (not a control inside it) shows/hides
  // the action bar. Desktop keeps pure hover; this state stays false there.
  const [actionsShown, setActionsShown] = useState(false);

  const onRowTap = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (!isCoarsePointer) return;
    const target = e.target as HTMLElement;
    if (target.closest("button, a, .md-spoiler") !== null) return;
    setActionsShown((v) => !v);
  };

  const remove = () => {
    if (window.confirm("Delete this message?")) {
      deleteMessage(message.id).catch((err: unknown) => {
        window.alert(
          err instanceof ApiError ? err.detail : "Failed to delete message",
        );
      });
    }
  };

  return (
    <div
      id={`msg-${message.id}`}
      className={`msg${withHeader ? " msg-head" : " msg-compact"}${
        actionsShown ? " actions-open" : ""
      }`}
      onClick={onRowTap}
    >
      <div className="msg-actions" aria-label="Message actions">
        <button title="Reply" onClick={() => onReply(message)}>
          ↩
        </button>
        <button
          title="Copy text"
          onClick={() => void navigator.clipboard.writeText(message.content)}
        >
          ⧉
        </button>
        {isMine && (
          <button title="Edit" onClick={() => onEdit(message)}>
            ✎
          </button>
        )}
        {isMine && (
          <button title="Delete" className="danger" onClick={remove}>
            🗑
          </button>
        )}
      </div>

      {message.reply_to_id !== null && (
        <ReplyRef original={original} onJumpTo={onJumpTo} />
      )}

      <div className="msg-main">
        <div className="msg-gutter">
          {withHeader ? (
            <AuthorAvatar message={message} />
          ) : (
            <span className="msg-hover-time">{shortTime(message.created_at)}</span>
          )}
        </div>
        <div className="msg-body">
          {withHeader && (
            <div className="msg-meta">
              <span className="msg-author">{message.author.name}</span>
              {message.author_type === "bot" && (
                <span className="bot-tag">BOT</span>
              )}
              <time
                className="msg-time"
                title={fullTime(message.created_at)}
                dateTime={message.created_at}
              >
                {shortTime(message.created_at)}
              </time>
              {hasReplies && (
                <span className="msg-replied" title="Has replies in this channel">
                  ↩ replied to
                </span>
              )}
            </div>
          )}
          {chibis[0] !== undefined && (
            <img className="chibi" src={chibis[0]} alt="" loading="lazy" />
          )}
          {hasText && (
            <div className="msg-content">
              <Markdown content={message.content} mentionNames={mentionNames} />
              {message.edited_at !== null && (
                <span className="msg-edited" title={fullTime(message.edited_at)}>
                  (edited)
                </span>
              )}
            </div>
          )}
          {!hasText && message.edited_at !== null && (
            <span className="msg-edited">(edited)</span>
          )}
          {chibis.slice(1).map((url) => (
            <img key={url} className="chibi" src={url} alt="" loading="lazy" />
          ))}
          {message.attachments.length > 0 && (
            <div className="msg-attachments">
              {message.attachments.map((att) => (
                <AttachmentItem key={att.id} att={att} onOpenImage={onOpenImage} />
              ))}
            </div>
          )}
          {unfurlUrl !== null && (
            <UnfurlCard url={unfurlUrl} onSummarize={onSummarize} />
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ list */

export interface MessageListProps {
  channelId: number;
  onReply: (m: Message) => void;
  onEdit: (m: Message) => void;
  onOpenImage: (att: Attachment) => void;
  onSummarize: (url: string) => void;
}

export function MessageList({
  channelId,
  onReply,
  onEdit,
  onOpenImage,
  onSummarize,
}: MessageListProps) {
  const cm = useMessages((s) => s.byChannel[channelId]);
  const me = useSession((s) => s.user);
  const members = useMembers((s) => s.byChannel[channelId]);

  const list = cm?.list ?? EMPTY_LIST;
  const loaded = cm?.loaded ?? false;
  const reachedStart = cm?.reachedStart ?? false;

  const containerRef = useRef<HTMLDivElement | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  const fetchingRef = useRef(false);
  const prependRef = useRef<{ height: number; top: number } | null>(null);
  const lastSeqRef = useRef(0);
  const [showJump, setShowJump] = useState(false);

  const items = useMemo(() => buildFeed(list), [list]);
  const byId = useMemo(() => new Map(list.map((m) => [m.id, m])), [list]);
  const repliedTo = useMemo(() => {
    // Client-side reverse index over the LOADED window only — WP4 has no
    // server-side reply reverse index, so older originals won't show this.
    const s = new Set<number>();
    for (const m of list) if (m.reply_to_id !== null) s.add(m.reply_to_id);
    return s;
  }, [list]);
  const mentionNames = useMemo(() => {
    if (members === undefined) return EMPTY_NAMES;
    const names: string[] = [];
    for (const m of members) names.push(m.name);
    return names;
  }, [members]);

  const scrollToBottom = () => {
    const el = containerRef.current;
    if (el !== null) el.scrollTop = el.scrollHeight;
  };

  const maybeFetchOlder = () => {
    const el = containerRef.current;
    if (el === null || fetchingRef.current || !loaded || reachedStart) return;
    fetchingRef.current = true;
    prependRef.current = { height: el.scrollHeight, top: el.scrollTop };
    void useMessages
      .getState()
      .fetchOlder(channelId)
      .catch(() => {
        prependRef.current = null;
      })
      .finally(() => {
        fetchingRef.current = false;
      });
  };

  // WP11: scrolling to the bottom of the active channel with the window
  // focused reads it (AppShell only marks read on channel switch / refocus).
  const lastMarkAtRef = useRef(0);
  const maybeMarkRead = () => {
    if (!document.hasFocus()) return;
    const st = useChannels.getState();
    if (st.activeChannelId !== channelId) return;
    const channel = st.channels.find((c) => c.id === channelId);
    if (channel === undefined || channel.unread === 0) return;
    const now = Date.now();
    if (now - lastMarkAtRef.current < MARK_READ_THROTTLE_MS) return;
    const seq = Math.max(
      useMessages.getState().lastSeq(channelId),
      channel.last_message?.seq ?? 0,
    );
    if (seq <= 0) return;
    lastMarkAtRef.current = now;
    void st.markRead(channelId, seq);
  };

  const onScroll = () => {
    const el = containerRef.current;
    if (el === null) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_STICK_PX;
    atBottomRef.current = atBottom;
    if (atBottom && showJump) setShowJump(false);
    if (atBottom) maybeMarkRead();
    if (el.scrollTop < TOP_FETCH_PX) maybeFetchOlder();
  };

  // Channel switch / first load: snap to the bottom, reset trackers.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (el === null) return;
    el.scrollTop = el.scrollHeight;
    atBottomRef.current = true;
    prependRef.current = null;
    lastSeqRef.current = list[list.length - 1]?.seq ?? 0;
    setShowJump(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelId, loaded]);

  // List changes: restore position after a prepend; stick or show the pill
  // when new messages append.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (el === null) return;
    if (prependRef.current !== null) {
      el.scrollTop =
        el.scrollHeight - prependRef.current.height + prependRef.current.top;
      prependRef.current = null;
    }
    const lastSeq = list[list.length - 1]?.seq ?? 0;
    if (lastSeq > lastSeqRef.current) {
      if (atBottomRef.current) scrollToBottom();
      else setShowJump(true);
    }
    lastSeqRef.current = lastSeq;
  }, [list]);

  // Late-loading images change scrollHeight; keep the view pinned when the
  // user is at the bottom (covers attachments, chibis, inline images).
  useEffect(() => {
    const el = containerRef.current;
    const inner = innerRef.current;
    if (el === null || inner === null) return;
    const ro = new ResizeObserver(() => {
      if (atBottomRef.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(inner);
    return () => ro.disconnect();
  }, [channelId]);

  const jumpToMessage = (id: number): boolean => {
    const el = document.getElementById(`msg-${id}`);
    if (el === null) return false;
    // Un-stick from the bottom so the ResizeObserver doesn't yank us back
    // down while late images load mid-jump.
    atBottomRef.current = false;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1600);
    return true;
  };

  // WP12 search jump: a store-level request targeting this channel. Retries
  // on every list change (the around-history page may still be landing);
  // cleared once the row exists and has been flashed.
  const jumpTarget = useMessages((s) => s.jumpTarget);
  useEffect(() => {
    if (jumpTarget === null || jumpTarget.channelId !== channelId) return;
    if (jumpToMessage(jumpTarget.messageId)) {
      useMessages.getState().clearJump();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jumpTarget, list, channelId]);

  return (
    <div className="message-scroll" ref={containerRef} onScroll={onScroll}>
      <div className="message-inner" ref={innerRef}>
        {!reachedStart && loaded && (
          <div className="scrollback-hint">Loading earlier messages…</div>
        )}
        {reachedStart && (
          <div className="channel-start">
            <p>This is the beginning of the channel.</p>
          </div>
        )}
        {!loaded && <div className="scrollback-hint">Loading messages…</div>}
        {items.map((item) =>
          item.kind === "divider" ? (
            <div className="day-divider" key={item.key}>
              <span>{item.label}</span>
            </div>
          ) : (
            <MessageRow
              key={item.key}
              message={item.message}
              withHeader={item.withHeader}
              isMine={
                me !== null &&
                item.message.author_type === "user" &&
                item.message.author_id === me.id
              }
              hasReplies={repliedTo.has(item.message.id)}
              original={
                item.message.reply_to_id !== null
                  ? byId.get(item.message.reply_to_id)
                  : undefined
              }
              mentionNames={mentionNames}
              onReply={onReply}
              onEdit={onEdit}
              onOpenImage={onOpenImage}
              onSummarize={onSummarize}
              onJumpTo={jumpToMessage}
            />
          ),
        )}
      </div>
      {showJump && (
        <button
          className="jump-pill"
          onClick={() => {
            scrollToBottom();
            setShowJump(false);
          }}
        >
          New messages ↓
        </button>
      )}
    </div>
  );
}

const EMPTY_NAMES: string[] = [];
