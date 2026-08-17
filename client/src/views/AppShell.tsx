import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api";
import { AddMembersModal } from "../components/AddMembersModal";
import { Avatar } from "../components/Avatar";
import { CheatSheet } from "../components/CheatSheet";
import { CreateChannelModal } from "../components/CreateChannelModal";
import { LockGlyph } from "../components/LockGlyph";
import { stripMarkdown } from "../components/Markdown";
import { MembershipNotice } from "../components/MembershipNotice";
import { SearchBar } from "../components/SearchBar";
import { UserPanel } from "../components/UserPanel";
import {
  channelIdFromHash,
  writeChannelHash,
} from "../hashRoute";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { useMembership } from "../stores/membership";
import { useMessages } from "../stores/messages";
import { usePresence } from "../stores/presence";
import { useSession } from "../stores/session";
import type { ChannelListItem, SettableStatus, UserStatus } from "../types";
import { isChannelMember, isPrivateChannel } from "../types";
import { socket } from "../ws";
import { ChatView } from "./ChatView";
import { SettingsView } from "./SettingsView";

const SETTINGS_HASH = "#/settings";

function PresenceDot({ userId }: { userId: number }) {
  const status = usePresence((s) => s.statuses[userId] ?? "offline");
  return <span className={`presence-dot ${status}`} />;
}

/** Member count in the channel header — private channels only, where "who is
    in here" is a real question (a public channel's answer is "everyone").
    Reads the roster ChatView already loaded; never fetches one itself. */
function MemberCount({ channelId }: { channelId: number }) {
  const members = useMembers((s) => s.byChannel[channelId]);
  if (members === undefined) return null;
  return (
    <span className="topbar-members" title="Members in this channel">
      👥 {members.length}
    </span>
  );
}

/** The private channel's own menu: invite (owner) and leave (anyone). Same
    scrim + popover pattern as the status picker in the footer. */
function ChannelMenu({
  isOwner,
  open,
  onToggle,
  onClose,
  onAddMembers,
  onLeave,
}: {
  isOwner: boolean;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  onAddMembers: () => void;
  onLeave: () => void;
}) {
  return (
    <span className="channel-menu-wrap">
      <button
        className="icon-btn channel-menu-btn"
        title="Channel options"
        aria-label="Channel options"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={onToggle}
      >
        ⋮
      </button>
      {open && (
        <>
          <div className="picker-scrim" onClick={onClose} />
          <div className="status-pop channel-menu" role="menu">
            {isOwner && (
              <button
                role="menuitem"
                className="status-option"
                onClick={onAddMembers}
              >
                Add members
              </button>
            )}
            <button
              role="menuitem"
              className="status-option danger"
              onClick={onLeave}
            >
              Leave channel
            </button>
          </div>
        </>
      )}
    </span>
  );
}

function ChannelRow({
  channel,
  active,
  onSelect,
}: {
  channel: ChannelListItem;
  active: boolean;
  onSelect: (id: number) => void;
}) {
  const isHashChannel = channel.type !== "dm_1to1"; // main_feed + text
  const isPrivate = isPrivateChannel(channel);
  // Only admins ever receive a row for a private channel they are not in: it
  // is listed (existence is not a secret) but carries no content, and clicking
  // it must not fetch any.
  const outsider = !isChannelMember(channel);
  const classes = [
    "channel-item",
    active ? "active" : "",
    channel.unread > 0 ? "unread" : "",
    outsider ? "not-member" : "",
  ]
    .filter(Boolean)
    .join(" ");
  // The server's snippet is the raw message content; markdown markers are
  // noise at preview size, so it is flattened to text (never markup — this is
  // user/bot-authored content and it goes in as a React text child).
  const preview =
    channel.last_message !== null
      ? stripMarkdown(channel.last_message.snippet)
      : "";
  return (
    <button
      className={classes}
      title={outsider ? "You're not a member of this channel" : undefined}
      onClick={() => onSelect(channel.id)}
    >
      {isHashChannel ? (
        isPrivate ? (
          <LockGlyph />
        ) : (
          <span className="hash">#</span>
        )
      ) : (
        channel.dm_user_id !== null && <PresenceDot userId={channel.dm_user_id} />
      )}
      <span className="channel-item-text">
        <span className="channel-item-top">
          <span className="name">{channel.name ?? "unnamed"}</span>
          {channel.unread > 0 && (
            <span className="unread-badge">
              {channel.unread > 99 ? "99+" : channel.unread}
            </span>
          )}
        </span>
        {preview.length > 0 && (
          <span className="channel-preview">{preview}</span>
        )}
      </span>
    </button>
  );
}

/* ---- footer: avatar + status popover + settings gear ---- */

const STATUS_OPTIONS: Array<{ value: SettableStatus; label: string }> = [
  { value: "online", label: "Online" },
  { value: "idle", label: "Idle" },
  { value: "dnd", label: "Do not disturb" },
];

function statusLabel(status: UserStatus): string {
  return STATUS_OPTIONS.find((o) => o.value === status)?.label ?? "Offline";
}

function UserFooter({ onOpenSettings }: { onOpenSettings: () => void }) {
  const user = useSession((s) => s.user);
  const [open, setOpen] = useState(false);
  if (user === null) return null;

  // sendStatus persists server-side (WS status op) AND broadcasts presence —
  // no PATCH needed. Mirror locally so our own UI updates instantly.
  const pickStatus = (status: SettableStatus) => {
    socket.sendStatus(status);
    useSession.getState().setUser({ ...user, status });
    usePresence.getState().setStatus(user.id, status);
    setOpen(false);
  };

  const shownStatus: UserStatus =
    user.status === "offline" ? "online" : user.status;

  return (
    <div className="user-footer">
      <Avatar src={user.avatar_url} name={user.display_name} />
      <div className="who">
        <span className="display-name">{user.display_name}</span>
        <button
          className="status-btn"
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span className={`presence-dot ${shownStatus}`} />
          {statusLabel(shownStatus)}
        </button>
      </div>
      {open && (
        <>
          <div className="picker-scrim" onClick={() => setOpen(false)} />
          <div className="status-pop" role="menu">
            {STATUS_OPTIONS.map((o) => (
              <button
                key={o.value}
                role="menuitem"
                className={`status-option${o.value === shownStatus ? " active" : ""}`}
                onClick={() => pickStatus(o.value)}
              >
                <span className={`presence-dot ${o.value}`} />
                {o.label}
              </button>
            ))}
          </div>
        </>
      )}
      <button className="icon-btn" title="Settings" onClick={onOpenSettings}>
        ⚙
      </button>
    </div>
  );
}

/* ---- shell ---- */

export function AppShell() {
  const channels = useChannels((s) => s.channels);
  const activeChannelId = useChannels((s) => s.activeChannelId);
  const loaded = useChannels((s) => s.loaded);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showSettings, setShowSettings] = useState(
    () => location.hash === SETTINGS_HASH,
  );
  const [membersOpen, setMembersOpen] = useState(
    () => window.innerWidth >= 1024,
  );
  const [cheatOpen, setCheatOpen] = useState(false);
  const [creatingChannel, setCreatingChannel] = useState(false);
  const [addingMembers, setAddingMembers] = useState(false);
  const [channelMenuOpen, setChannelMenuOpen] = useState(false);
  const me = useSession((s) => s.user);
  const showSettingsRef = useRef(showSettings);
  showSettingsRef.current = showSettings;

  // Boot: load the sidebar, open the socket, adopt a deep-linked route.
  useEffect(() => {
    const st = useChannels.getState();
    void st.refresh();
    if (location.hash !== SETTINGS_HASH) st.setActive(channelIdFromHash());
    socket.connect();
    // Route changes from outside (notification deep-links, back button).
    const onHash = () => {
      if (location.hash === SETTINGS_HASH) {
        setShowSettings(true);
        return;
      }
      setShowSettings(false);
      const id = channelIdFromHash();
      if (id !== null) useChannels.getState().setActive(id);
    };
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
      socket.disconnect();
    };
  }, []);

  // Default to #main once channels arrive (unless a deep link chose one).
  useEffect(() => {
    if (!loaded) return;
    const st = useChannels.getState();
    if (st.activeChannelId === null) {
      const main = st.channels.find((c) => c.type === "main_feed");
      if (main !== undefined) st.setActive(main.id);
    }
  }, [loaded]);

  // Seed presence dots from stored member statuses (live frames overwrite).
  useEffect(() => {
    if (!loaded) return;
    const main = useChannels.getState().channels.find(
      (c) => c.type === "main_feed",
    );
    if (main === undefined) return;
    // Through the members store, not a bare listMembers: the main feed is
    // public and lists every user, so this roster doubles as the client's user
    // directory — who invited me, who I can invite.
    void useMembers
      .getState()
      .refresh(main.id)
      .then(() => {
        const presence = usePresence.getState();
        for (const m of useMembers.getState().byChannel[main.id] ?? []) {
          if (m.type === "user" && m.status != null) {
            presence.setStatus(m.id, m.status);
          }
        }
      });
  }, [loaded]);

  // Channel switch: sync hash, tell the server our focus (push suppression),
  // load history, clear the unread badge.
  useEffect(() => {
    if (!showSettingsRef.current) {
      writeChannelHash(activeChannelId);
      socket.sendFocus(activeChannelId);
    }
    setSidebarOpen(false);
    setChannelMenuOpen(false);
    if (activeChannelId === null) return;
    /* Fetch nothing until the sidebar has landed and says we may read this
       channel. A deep link (#/channels/7) can name a private channel we are
       not in — admins see those rows, and everyone can type a URL — and
       history + read-state both answer 403 there. `loaded` is in the deps, so
       this re-runs the moment the list arrives. ChatView shows the "not a
       member" placeholder in the meantime. */
    const list = useChannels.getState();
    if (!list.loaded) return;
    const row = list.channels.find((c) => c.id === activeChannelId);
    if (row === undefined || !isChannelMember(row)) return;
    void useMessages
      .getState()
      .ensureLoaded(activeChannelId)
      .then(() => {
        const st = useChannels.getState();
        const channel = st.channels.find((c) => c.id === activeChannelId);
        const seq = Math.max(
          useMessages.getState().lastSeq(activeChannelId),
          channel?.last_message?.seq ?? 0,
        );
        if (seq > 0 && (channel === undefined || channel.unread > 0)) {
          void st.markRead(activeChannelId, seq);
        }
      });
  }, [activeChannelId, loaded]);

  // Settings open/close: while in settings the user is not reading the
  // channel, so drop server-side focus (pushes for it resume).
  useEffect(() => {
    if (showSettings) {
      socket.sendFocus(null);
    } else {
      socket.sendFocus(useChannels.getState().activeChannelId);
    }
  }, [showSettings]);

  // Window blur/focus: keep server-side focus accurate — notification
  // suppression depends on it (spec: send on EVERY blur/focus).
  useEffect(() => {
    const onBlur = () => socket.sendFocus(null);
    const onFocus = () => {
      if (showSettingsRef.current) return; // settings = no channel focused
      const st = useChannels.getState();
      socket.sendFocus(st.activeChannelId);
      // Returning to the window reads the visible channel.
      const channel = st.channels.find((c) => c.id === st.activeChannelId);
      if (
        channel !== undefined &&
        isChannelMember(channel) &&
        channel.unread > 0
      ) {
        const seq = Math.max(
          useMessages.getState().lastSeq(channel.id),
          channel.last_message?.seq ?? 0,
        );
        if (seq > 0) void st.markRead(channel.id, seq);
      }
    };
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  // Document title carries the total unread count: "(3) Disjorn".
  const totalUnread = channels.reduce((sum, c) => sum + c.unread, 0);
  useEffect(() => {
    document.title = totalUnread > 0 ? `(${totalUnread}) Disjorn` : "Disjorn";
  }, [totalUnread]);

  const openSettings = () => {
    setSidebarOpen(false);
    setShowSettings(true);
    history.replaceState(null, "", SETTINGS_HASH);
  };
  const closeSettings = () => {
    setShowSettings(false);
    writeChannelHash(useChannels.getState().activeChannelId);
  };

  const active = channels.find((c) => c.id === activeChannelId);
  const dms = channels.filter((c) => c.type === "dm_1to1");
  const mains = channels.filter((c) => c.type !== "dm_1to1"); // main_feed + text
  const select = (id: number) => {
    if (showSettingsRef.current) closeSettings();
    useChannels.getState().setActive(id);
    // Same channel clicked while in settings: the effect won't re-fire.
    writeChannelHash(id);
    setSidebarOpen(false);
  };

  /* Create flow: CreateChannelModal owns name + public/private and, for a
     private channel, the "add members" step that follows creation. The
     channel_create WS frame keeps everyone else's sidebar live (for a private
     channel it reaches its members only — at that moment, its owner). */

  const activePrivate = active !== undefined && isPrivateChannel(active);
  /* Fail OPEN on an unknown row (the list is still loading): the member panel
     shouldn't blink out of existence on every boot. The reads it would make
     are guarded where they happen, not here. */
  const activeMember = active === undefined || isChannelMember(active);
  const isOwner =
    me !== null && active?.created_by != null && active.created_by === me.id;

  const leaveActive = () => {
    if (active === undefined) return;
    setChannelMenuOpen(false);
    const label = `#${active.name ?? ""}`;
    const warning = isOwner
      ? `Leave ${label}? You stay its owner, but you lose access to it — ` +
        `including everything already posted — until you add yourself back.`
      : `Leave ${label}? You lose access to it, including everything already ` +
        `posted. Only its owner can let you back in.`;
    if (!window.confirm(warning)) return;
    useMembership
      .getState()
      .leave(active.id)
      .catch((err: unknown) => {
        window.alert(
          err instanceof ApiError ? err.detail : "Failed to leave the channel",
        );
      });
  };

  return (
    <div className="shell">
      {sidebarOpen && (
        <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)} />
      )}
      <nav className={`sidebar${sidebarOpen ? " open" : ""}`}>
        <div className="sidebar-header">Disjorn</div>
        <div className="channel-list">
          <div className="channel-section channel-section-row">
            <span>Channels</span>
            <button
              className="icon-btn add-channel-btn"
              title="Create channel"
              aria-label="Create channel"
              onClick={() => setCreatingChannel(true)}
            >
              +
            </button>
          </div>
          {mains.map((c) => (
            <ChannelRow
              key={c.id}
              channel={c}
              active={c.id === activeChannelId && !showSettings}
              onSelect={select}
            />
          ))}
          <div className="channel-section">Direct messages</div>
          {dms.length === 0 && (
            <span className="channel-section" style={{ textTransform: "none" }}>
              No DMs yet
            </span>
          )}
          {dms.map((c) => (
            <ChannelRow
              key={c.id}
              channel={c}
              active={c.id === activeChannelId && !showSettings}
              onSelect={select}
            />
          ))}
        </div>
        <UserFooter onOpenSettings={openSettings} />
      </nav>
      <main className="main-panel">
        {showSettings ? (
          <SettingsView onClose={closeSettings} />
        ) : (
          <>
            <header className="topbar">
              <button
                className="icon-btn hamburger"
                aria-label="Open channel list"
                onClick={() => setSidebarOpen(true)}
              >
                ☰
              </button>
              <span className="title">
                {active !== undefined ? (
                  <>
                    {active.type !== "dm_1to1" &&
                      (activePrivate ? (
                        <LockGlyph />
                      ) : (
                        <span className="hash">#</span>
                      ))}
                    {active.name}
                  </>
                ) : (
                  "Disjorn"
                )}
              </span>
              {activePrivate && activeMember && (
                <MemberCount channelId={active.id} />
              )}
              {activePrivate && activeMember && (
                <ChannelMenu
                  isOwner={isOwner}
                  open={channelMenuOpen}
                  onToggle={() => setChannelMenuOpen((v) => !v)}
                  onClose={() => setChannelMenuOpen(false)}
                  onAddMembers={() => {
                    setChannelMenuOpen(false);
                    setAddingMembers(true);
                  }}
                  onLeave={leaveActive}
                />
              )}
              <SearchBar />
              {me?.is_admin && (
                <button
                  className="icon-btn cheat-toggle"
                  title="Command cheat sheet"
                  aria-label="Command cheat sheet"
                  onClick={() => setCheatOpen(true)}
                >
                  ⌘
                </button>
              )}
              {activeChannelId !== null && activeMember && (
                <button
                  className={`icon-btn members-toggle${membersOpen ? " active" : ""}`}
                  title={membersOpen ? "Hide member list" : "Show member list"}
                  aria-pressed={membersOpen}
                  onClick={() => setMembersOpen((v) => !v)}
                >
                  👥
                </button>
              )}
            </header>
            <div className="chat-with-members">
              <ChatView />
              {membersOpen && activeChannelId !== null && activeMember && (
                <>
                  <div
                    className="member-scrim"
                    onClick={() => setMembersOpen(false)}
                  />
                  <UserPanel
                    channelId={activeChannelId}
                    onNavigate={() => {
                      if (window.innerWidth < 1024) setMembersOpen(false);
                    }}
                  />
                </>
              )}
            </div>
          </>
        )}
      </main>
      {cheatOpen && me?.is_admin && (
        <CheatSheet onClose={() => setCheatOpen(false)} />
      )}
      {creatingChannel && (
        <CreateChannelModal
          onCreated={(created) => select(created.id)}
          onClose={() => setCreatingChannel(false)}
        />
      )}
      {addingMembers && active !== undefined && isOwner && (
        <AddMembersModal
          channelId={active.id}
          channelName={active.name ?? ""}
          onAdded={() => void useMembers.getState().refresh(active.id)}
          onClose={() => setAddingMembers(false)}
        />
      )}
      {/* Added to / removed from a private channel — a modal for now. */}
      <MembershipNotice onOpenChannel={select} />
    </div>
  );
}
