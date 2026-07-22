/* Member panel (WP11): roster for the active channel — right column on
   desktop, slide-in sheet on mobile (AppShell owns the open/close state and
   the breakpoint styling; this component just renders the list).

   Click a human (not yourself) -> open/create the 1:1 DM and jump to it.

   Bot membership is managed from here (add via AddBotModal, remove inline).
   The affordance is offered only when the viewer appears in this channel's own
   roster — which for a DM means being one of its two participants, exactly the
   condition POST/DELETE /channels/{id}/bots enforces server-side. The server is
   the wall; this is just refusing to show a button that would only 403. */

import { useEffect, useMemo, useState } from "react";

import { ApiError, removeChannelBot } from "../api";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { usePresence } from "../stores/presence";
import { useSession } from "../stores/session";
import type { ChannelMemberOut, UserStatus } from "../types";
import { AddBotModal } from "./AddBotModal";
import { Avatar, BotAvatar } from "./Avatar";

const STATUS_ORDER: Record<UserStatus, number> = {
  online: 0,
  idle: 1,
  dnd: 2,
  offline: 3,
};

const STATUS_LABEL: Record<UserStatus, string> = {
  online: "Online",
  idle: "Idle",
  dnd: "Do not disturb",
  offline: "Offline",
};

function MemberRow({
  member,
  status,
  isSelf,
  onOpenDm,
  onRemove,
}: {
  member: ChannelMemberOut;
  status: UserStatus | null; // null for bots
  isSelf: boolean;
  onOpenDm: (userId: number) => void;
  /** Bots only, and only when the viewer may manage them. */
  onRemove?: () => void;
}) {
  const isBot = member.type === "bot";
  const clickable = !isBot && !isSelf;
  const body = (
    <>
      <span className="member-avatar">
        {isBot ? (
          <BotAvatar botId={member.id} name={member.name} />
        ) : (
          <Avatar userId={member.id} name={member.name} />
        )}
        {status !== null && (
          <span className={`presence-dot ring ${status}`} aria-label={STATUS_LABEL[status]} />
        )}
      </span>
      <span className="member-name">
        {member.name}
        {isSelf && <span className="member-you"> (you)</span>}
      </span>
      {isBot && <span className="bot-tag">BOT</span>}
    </>
  );

  if (clickable) {
    return (
      <button
        className="member-row"
        title={`Message ${member.name}`}
        onClick={() => onOpenDm(member.id)}
      >
        {body}
      </button>
    );
  }
  // Static rows are a <div>, not a disabled <button>: a bot row can carry its
  // own remove control, and buttons don't nest.
  return (
    <div
      className="member-row static"
      title={status !== null ? STATUS_LABEL[status] : undefined}
    >
      {body}
      {onRemove !== undefined && (
        <button
          className="member-remove"
          title={`Remove ${member.name} from this channel`}
          aria-label={`Remove ${member.name} from this channel`}
          onClick={onRemove}
        >
          ✕
        </button>
      )}
    </div>
  );
}

export function UserPanel({
  channelId,
  onNavigate,
}: {
  channelId: number;
  /** Called after a DM jump (mobile sheet closes itself via this). */
  onNavigate: () => void;
}) {
  const members = useMembers((s) => s.byChannel[channelId]);
  const statuses = usePresence((s) => s.statuses);
  const me = useSession((s) => s.user);
  const channel = useChannels((s) => s.channels.find((c) => c.id === channelId));
  const [addingBot, setAddingBot] = useState(false);

  // Fresh roster whenever the panel is shown for a channel (statuses in the
  // roster payload seed presence for users we haven't seen frames for).
  useEffect(() => {
    void useMembers.getState().refresh(channelId);
  }, [channelId]);

  const statusFor = (m: ChannelMemberOut): UserStatus =>
    statuses[m.id] ?? m.status ?? "offline";

  const { users, bots } = useMemo(() => {
    const all = members ?? [];
    const users = all
      .filter((m) => m.type === "user")
      .sort((a, b) => {
        const byStatus = STATUS_ORDER[statusFor(a)] - STATUS_ORDER[statusFor(b)];
        return byStatus !== 0 ? byStatus : a.name.localeCompare(b.name);
      });
    const bots = all
      .filter((m) => m.type === "bot")
      .sort((a, b) => a.name.localeCompare(b.name));
    return { users, bots };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [members, statuses]);

  const openDm = (userId: number) => {
    void useChannels
      .getState()
      .openDm(userId)
      .then(() => onNavigate())
      .catch(() => {
        /* transient — the row simply stays put */
      });
  };

  /* The roster IS the membership test: main_feed and text channels list every
     user, a DM lists exactly its two participants. Finding yourself in it is
     therefore the same question the server asks before it will touch bot
     membership — and the roster is only served to members in the first place. */
  const isDm = channel?.type === "dm_1to1";
  const canManageBots =
    me !== null && members !== undefined && users.some((u) => u.id === me.id);
  const channelLabel =
    channel === undefined
      ? "this channel"
      : `${isDm ? "@" : "#"}${channel.name ?? ""}`;

  const removeBot = (bot: ChannelMemberOut) => {
    const consequence = isDm
      ? `Remove ${bot.name} from this DM? It stops receiving new messages here. Anything it has already read, it has already read.`
      : `Remove ${bot.name} from ${channelLabel}?`;
    if (!window.confirm(consequence)) return;
    removeChannelBot(channelId, bot.id).then(
      () => void useMembers.getState().refresh(channelId),
      (err: unknown) => {
        window.alert(
          err instanceof ApiError ? err.detail : "Failed to remove the bot",
        );
      },
    );
  };

  return (
    <>
      <aside className="member-panel" aria-label="Channel members">
        <div className="member-section">Members — {users.length + bots.length}</div>
        {members === undefined && <div className="member-hint">Loading…</div>}
        {users.map((m) => (
          <MemberRow
            key={`u${m.id}`}
            member={m}
            status={statusFor(m)}
            isSelf={me !== null && m.id === me.id}
            onOpenDm={openDm}
          />
        ))}
        {(bots.length > 0 || canManageBots) && (
          <div className="member-section member-section-row">
            <span>Bots</span>
            {canManageBots && (
              <button
                className="icon-btn add-bot-btn"
                title="Add a bot to this channel"
                aria-label="Add a bot to this channel"
                onClick={() => setAddingBot(true)}
              >
                +
              </button>
            )}
          </div>
        )}
        {bots.length === 0 && canManageBots && (
          <div className="member-hint">
            {isDm ? "No bots — this DM is between you two." : "No bots here."}
          </div>
        )}
        {bots.map((m) => (
          <MemberRow
            key={`b${m.id}`}
            member={m}
            status={null}
            isSelf={false}
            onOpenDm={openDm}
            onRemove={canManageBots ? () => removeBot(m) : undefined}
          />
        ))}
      </aside>
      {/* Sibling, not a child: the panel is a fixed, scrolling column on
          mobile and a modal has no business living inside it. */}
      {addingBot && canManageBots && (
        <AddBotModal
          channelId={channelId}
          channelLabel={channelLabel}
          isDm={isDm}
          existingBotIds={bots.map((b) => b.id)}
          onAdded={() => void useMembers.getState().refresh(channelId)}
          onClose={() => setAddingBot(false)}
        />
      )}
    </>
  );
}
