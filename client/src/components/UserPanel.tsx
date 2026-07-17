/* Member panel (WP11): roster for the active channel — right column on
   desktop, slide-in sheet on mobile (AppShell owns the open/close state and
   the breakpoint styling; this component just renders the list).

   Click a human (not yourself) -> open/create the 1:1 DM and jump to it. */

import { useEffect, useMemo } from "react";

import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { usePresence } from "../stores/presence";
import { useSession } from "../stores/session";
import type { ChannelMemberOut, UserStatus } from "../types";
import { Avatar } from "./Avatar";

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
}: {
  member: ChannelMemberOut;
  status: UserStatus | null; // null for bots
  isSelf: boolean;
  onOpenDm: (userId: number) => void;
}) {
  const isBot = member.type === "bot";
  const clickable = !isBot && !isSelf;
  return (
    <button
      className={`member-row${clickable ? "" : " static"}`}
      disabled={!clickable}
      title={
        clickable
          ? `Message ${member.name}`
          : status !== null
            ? STATUS_LABEL[status]
            : undefined
      }
      onClick={() => {
        if (clickable) onOpenDm(member.id);
      }}
    >
      <span className="member-avatar">
        {isBot ? (
          <div className="avatar avatar-bot" aria-hidden>
            {member.name.slice(0, 1).toUpperCase()}
          </div>
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
    </button>
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

  return (
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
      {bots.length > 0 && <div className="member-section">Bots</div>}
      {bots.map((m) => (
        <MemberRow
          key={`b${m.id}`}
          member={m}
          status={null}
          isSelf={false}
          onOpenDm={openDm}
        />
      ))}
    </aside>
  );
}
