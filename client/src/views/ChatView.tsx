/* Chat panel (WP10): message feed + typing line + composer + modals.

   AppShell owns the channel-switch side effects (ensureLoaded, mark-read,
   socket.sendFocus) — nothing here re-fires those. This view owns the
   reply/edit composer state, the image-preview modal, and the summarize
   modal, and loads the member roster for mention/typing name resolution. */

import { useEffect, useState } from "react";

import { Composer } from "../components/Composer";
import { ImageModal } from "../components/ImageModal";
import { ChannelLabel } from "../components/LockGlyph";
import { MessageList } from "../components/MessageList";
import { SummarizeModal } from "../components/SummarizeModal";
import { TypingLine } from "../components/TypingLine";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import type { Attachment, Message } from "../types";
import { isChannelMember, isPrivateChannel } from "../types";

export function ChatView() {
  const activeChannelId = useChannels((s) => s.activeChannelId);
  const channel = useChannels((s) =>
    s.channels.find((c) => c.id === s.activeChannelId),
  );

  const [replyTo, setReplyTo] = useState<Message | null>(null);
  const [editing, setEditing] = useState<Message | null>(null);
  const [imageAtt, setImageAtt] = useState<Attachment | null>(null);
  const [summarizeTarget, setSummarizeTarget] = useState<string | null>(null);

  /* A private channel we are not in: the row is visible (admins see them all),
     the content is not. Every read path for it answers 403, so this view
     fetches NOTHING for it — no roster, no history, no mark-read.

     `canRead` is deliberately fail-closed AND a dependency: while the sidebar
     is still loading, the row is unknown, nothing is fetched, and the effect
     re-runs the moment the row (and with it the answer) arrives. */
  const walled = channel !== undefined && !isChannelMember(channel);
  const canRead = channel !== undefined && isChannelMember(channel);

  // Channel switch resets transient composer state and loads the roster.
  useEffect(() => {
    setReplyTo(null);
    setEditing(null);
    setImageAtt(null);
    setSummarizeTarget(null);
    if (activeChannelId !== null && canRead) {
      void useMembers.getState().ensureLoaded(activeChannelId);
    }
  }, [activeChannelId, canRead]);

  if (activeChannelId === null) {
    return (
      <div className="chat-placeholder">
        <p>Select a channel to start chatting.</p>
      </div>
    );
  }

  if (walled) {
    return (
      <div className="chat-placeholder">
        <p>
          You're not a member of{" "}
          <ChannelLabel
            name={channel?.name ?? ""}
            isPrivate={isPrivateChannel(channel)}
          />{" "}
          — ask its owner.
        </p>
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
