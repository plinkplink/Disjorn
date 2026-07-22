/* Chat panel (WP10): message feed + typing line + composer + modals.

   AppShell owns the channel-switch side effects (ensureLoaded, mark-read,
   socket.sendFocus) — nothing here re-fires those. This view owns the
   reply/edit composer state, the image-preview modal, and the summarize
   modal, and loads the member roster for mention/typing name resolution. */

import { useEffect, useState } from "react";

import { Composer } from "../components/Composer";
import { ImageModal } from "../components/ImageModal";
import { MessageList } from "../components/MessageList";
import { SummarizeModal } from "../components/SummarizeModal";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { usePresence } from "../stores/presence";
import { useSession } from "../stores/session";
import type { Attachment, Message } from "../types";

function TypingLine({ channelId }: { channelId: number }) {
  const typists = usePresence((s) => s.typing[channelId]);
  const me = useSession((s) => s.user);
  const members = useMembers((s) => s.byChannel[channelId]);

  const others = (typists ?? []).filter(
    (t) => !(t.authorType === "user" && me !== null && t.authorId === me.id),
  );

  let text = "";
  if (others.length > 0) {
    const names = others.map(
      (t) =>
        members?.find((m) => m.type === t.authorType && m.id === t.authorId)
          ?.name ?? "Someone",
    );
    if (names.length === 1) text = `${names[0] ?? "Someone"} is typing`;
    else if (names.length === 2) text = `${names[0]} and ${names[1]} are typing`;
    else text = "Several people are typing";
  }

  // Fixed-height line: reserves space so the feed doesn't jump.
  return (
    <div className="typing-line" aria-live="polite">
      {text.length > 0 && (
        <>
          <span className="typing-dots" aria-hidden>
            <i />
            <i />
            <i />
          </span>
          {text}…
        </>
      )}
    </div>
  );
}

export function ChatView() {
  const activeChannelId = useChannels((s) => s.activeChannelId);
  const channel = useChannels((s) =>
    s.channels.find((c) => c.id === s.activeChannelId),
  );

  const [replyTo, setReplyTo] = useState<Message | null>(null);
  const [editing, setEditing] = useState<Message | null>(null);
  const [imageAtt, setImageAtt] = useState<Attachment | null>(null);
  const [summarizeTarget, setSummarizeTarget] = useState<string | null>(null);

  // Channel switch resets transient composer state and loads the roster.
  useEffect(() => {
    setReplyTo(null);
    setEditing(null);
    setImageAtt(null);
    setSummarizeTarget(null);
    if (activeChannelId !== null) {
      void useMembers.getState().ensureLoaded(activeChannelId);
    }
  }, [activeChannelId]);

  if (activeChannelId === null) {
    return (
      <div className="chat-placeholder">
        <p>Select a channel to start chatting.</p>
      </div>
    );
  }

  const channelName =
    channel !== undefined
      ? `${channel.type !== "dm_1to1" ? "#" : "@"}${channel.name ?? ""}`
      : "";

  return (
    <div className="chat-view">
      <MessageList
        channelId={activeChannelId}
        onReply={(m) => {
          setEditing(null);
          setReplyTo(m);
        }}
        onEdit={(m) => {
          setReplyTo(null);
          setEditing(m);
        }}
        onOpenImage={setImageAtt}
        onSummarize={setSummarizeTarget}
      />
      <TypingLine channelId={activeChannelId} />
      <Composer
        channelId={activeChannelId}
        channelName={channelName}
        replyTo={replyTo}
        onCancelReply={() => setReplyTo(null)}
        editing={editing}
        onStartEdit={(m) => {
          setReplyTo(null);
          setEditing(m);
        }}
        onCancelEdit={() => setEditing(null)}
      />
      {imageAtt !== null && imageAtt.url !== null && (
        <ImageModal
          src={imageAtt.url}
          origUrl={imageAtt.orig_url}
          filename={imageAtt.original_filename}
          onClose={() => setImageAtt(null)}
        />
      )}
      {summarizeTarget !== null && (
        <SummarizeModal
          url={summarizeTarget}
          channelId={activeChannelId}
          onClose={() => setSummarizeTarget(null)}
        />
      )}
    </div>
  );
}
