import { useEffect, useRef, useState } from "react";

import { ApiError, createChannel, listMembers } from "../api";
import { Avatar } from "../components/Avatar";
import { CheatSheet } from "../components/CheatSheet";
import { stripMarkdown } from "../components/Markdown";
import { SearchBar } from "../components/SearchBar";
import { UserPanel } from "../components/UserPanel";
import {
  channelIdFromHash,
  writeChannelHash,
} from "../hashRoute";
import { useChannels } from "../stores/channels";
import { useMessages } from "../stores/messages";
import { usePresence } from "../stores/presence";
import { useSession } from "../stores/session";
import type { ChannelListItem, SettableStatus, UserStatus } from "../types";
import { socket } from "../ws";
import { ChatView } from "./ChatView";
import { SettingsView } from "./SettingsView";

const SETTINGS_HASH = "#/settings";

function PresenceDot({ userId }: { userId: number }) {
  const status = usePresence((s) => s.statuses[userId] ?? "offline");
  return <span className={`presence-dot ${status}`} />;
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
  const classes = [
    "channel-item",
    active ? "active" : "",
    channel.unread > 0 ? "unread" : "",
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
    <button className={classes} onClick={() => onSelect(channel.id)}>
      {isHashChannel ? (
        <span className="hash">#</span>
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
      <Avatar userId={user.id} name={user.display_name} />
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
    void listMembers(main.id).then((members) => {
      const presence = usePresence.getState();
      for (const m of members) {
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
    if (activeChannelId === null) return;
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
  }, [activeChannelId]);

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
      if (channel !== undefined && channel.unread > 0) {
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

  // Minimal v1 create flow: browser prompt -> POST /channels -> refetch.
  // The channel_create WS frame keeps everyone else's sidebar live.
  const addChannel = async () => {
    const raw = window.prompt(
      "New channel name (1-32 chars: lowercase a-z, 0-9, dashes):",
    );
    if (raw === null) return;
    const name = raw.trim().toLowerCase();
    if (name === "") return;
    try {
      const created = await createChannel(name);
      await useChannels.getState().refresh();
      select(created.id);
    } catch (err) {
      window.alert(
        err instanceof ApiError ? err.detail : "Failed to create channel",
      );
    }
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
              onClick={() => void addChannel()}
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
                    {active.type !== "dm_1to1" && <span className="hash">#</span>}
                    {active.name}
                  </>
                ) : (
                  "Disjorn"
                )}
              </span>
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
              {activeChannelId !== null && (
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
              {membersOpen && activeChannelId !== null && (
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
    </div>
  );
}
