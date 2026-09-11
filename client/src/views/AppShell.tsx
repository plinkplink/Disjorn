import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api";
import { AddMembersModal } from "../components/AddMembersModal";
import { AppBuildModal } from "../components/AppBuildModal";
import { AppsChooserModal } from "../components/AppsChooserModal";
import { Avatar, BotAvatar } from "../components/Avatar";
import { ChannelDeletedToast } from "../components/ChannelDeletedToast";
import { CheatSheet } from "../components/CheatSheet";
import { CreateChannelModal } from "../components/CreateChannelModal";
import { DeleteChannelModal } from "../components/DeleteChannelModal";
import { LockGlyph } from "../components/LockGlyph";
import { stripMarkdown } from "../components/Markdown";
import { MembershipNotice } from "../components/MembershipNotice";
import { SearchBar } from "../components/SearchBar";
import { UserPanel } from "../components/UserPanel";
import {
  channelIdFromHash,
  writeChannelHash,
} from "../hashRoute";
import { POPUP_BLOCKED_NOTE, openMinted } from "../lib/openMinted";
import { useApps } from "../stores/apps";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { useMembership } from "../stores/membership";
import { useMessages } from "../stores/messages";
import { usePresence } from "../stores/presence";
import { useSession } from "../stores/session";
import type {
  App,
  Builder,
  ChannelListItem,
  SettableStatus,
  UserStatus,
} from "../types";
import { isChannelMember, isPrivateChannel } from "../types";
import { socket } from "../ws";
import { ChatView } from "./ChatView";
import PlanRoomView from "./PlanRoomView";
import { SettingsView } from "./SettingsView";

const SETTINGS_HASH = "#/settings";
const PLANROOM_HASH = "#/planroom";

/* Which full-panel view is covering the chat, if any. A discriminated value
   rather than one boolean per view: the shell has three "am I in a channel?"
   decisions (server-side focus, window refocus, the sidebar's active row) and
   every one of them means "no overlay", not "not settings". A second boolean
   would have had to be added to all three by hand, and the one that got missed
   would be a silent bug — the server going on believing you are reading a
   channel you are not looking at. */
type Overlay = "none" | "settings" | "planroom";

function overlayFromHash(hash: string = location.hash): Overlay {
  if (hash === SETTINGS_HASH) return "settings";
  if (hash === PLANROOM_HASH) return "planroom";
  return "none";
}

/* Channels the house cannot lose — the server refuses to delete these (400;
   PROTECTED_CHANNEL_NAMES in server/app/routers/channels.py is the authority),
   so the Delete item is never offered for them. */
const PROTECTED_CHANNEL_NAMES = new Set(["main", "custodian"]);

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

/** The channel's own menu: invite (private, owner), leave (private, member)
    and delete (text channel, owner or admin). Same scrim + popover pattern as
    the status picker in the footer.

    Each item is passed in as a flag rather than derived here, because the
    three answer to different rules — membership for the first two, ownership
    or the admin bit for the third — and only the shell knows all of them. */
function ChannelMenu({
  canAddMembers,
  canLeave,
  canDelete,
  open,
  onToggle,
  onClose,
  onAddMembers,
  onLeave,
  onDelete,
}: {
  canAddMembers: boolean;
  canLeave: boolean;
  canDelete: boolean;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  onAddMembers: () => void;
  onLeave: () => void;
  onDelete: () => void;
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
            {canAddMembers && (
              <button
                role="menuitem"
                className="status-option"
                onClick={onAddMembers}
              >
                Add members
              </button>
            )}
            {canLeave && (
              <button
                role="menuitem"
                className="status-option danger"
                onClick={onLeave}
              >
                Leave channel
              </button>
            )}
            {canDelete && (
              <>
                {/* Kept apart from the reversible items above it: this one
                    takes the history with it. */}
                {(canAddMembers || canLeave) && (
                  <div className="menu-sep" role="separator" />
                )}
                <button
                  role="menuitem"
                  className="status-option danger"
                  onClick={onDelete}
                >
                  Delete channel
                </button>
              </>
            )}
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

/* ---- apps ---- */

/**
 * One row of the APPS group. The avatar is the BUILDER's, not the app's: an
 * app has no face of its own in v1, and the seat that built it is the honest
 * thing to show. An unknown builder (a seat since removed from the roster)
 * still gets a row — the app did not stop existing.
 */
function AppRow({
  app,
  builder,
  onOpen,
}: {
  app: App;
  builder: Builder | undefined;
  onOpen: (app: App) => void;
}) {
  const building = app.open_session !== null;
  return (
    <button className="channel-item app-row" onClick={() => onOpen(app)}>
      <BotAvatar
        src={builder?.avatar_url}
        name={builder?.name ?? app.name}
        size={20}
      />
      <span className="channel-item-text">
        <span className="name">{app.name}</span>
      </span>
      {building && (
        <span className="app-building" title="A build session is open">
          <span className="app-building-dot" aria-hidden />
          building…
        </span>
      )}
      <span className={`app-status-chip ${app.status}`}>{app.status}</span>
    </button>
  );
}

/**
 * The read-only card for an app you cannot open a session on — someone else's,
 * or one of yours that is past `draft`. Open is rendered disabled rather than
 * hidden while the app is not live: the app IS reachable eventually, and
 * saying when is more use than pretending the button was never designed.
 *
 * Share is not here. Sharing is the owner's verb and it lives in the build
 * modal beside Live, where the thing being shared is on screen (stage 3 D7).
 */
function AppCardModal({
  app,
  builder,
  onClose,
}: {
  app: App;
  builder: Builder | undefined;
  onClose: () => void;
}) {
  const originBase = useApps((s) => s.originBase);
  const [opening, setOpening] = useState(false);
  const [remixing, setRemixing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The minted URL when the browser refused the tab — rendered as a link the
      user can click, which is a gesture no popup blocker argues with. */
  const [blockedUrl, setBlockedUrl] = useState<string | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  /* Synchronous tab claim, then navigate when the mint lands — the shared
     helper, for the same reason the other two Open buttons use it. */
  const open = () => {
    if (opening) return;
    setOpening(true);
    setError(null);
    setBlockedUrl(null);
    void openMinted(() => useApps.getState().openApp(app.id, "live")).then(
      (result) => {
        setOpening(false);
        if (result.kind === "blocked") setBlockedUrl(result.url);
        if (result.kind === "failed") {
          setError(
            result.error instanceof ApiError
              ? result.error.detail
              : "Could not open it",
          );
        }
      },
    );
  };

  const remix = () => {
    if (remixing) return;
    setRemixing(true);
    setError(null);
    setBlockedUrl(null);
    useApps
      .getState()
      .remixApp(app.id)
      .then(
        (session) => {
          setRemixing(false);
          // The shell closes this card and opens the build modal on the copy.
          useApps.getState().requestBuildModal(session.id);
        },
        (err: unknown) => {
          setRemixing(false);
          setError(err instanceof ApiError ? err.detail : "Could not remix it");
        },
      );
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="create-channel-modal"
        role="dialog"
        aria-modal="true"
        aria-label={app.name}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head">
          <span className="member-modal-title">{app.name}</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <p className="member-modal-note">
          {app.description.length > 0
            ? app.description
            : "No description yet."}
        </p>
        <div className="app-card-meta">
          <BotAvatar
            src={builder?.avatar_url}
            name={builder?.name ?? app.name}
            size={24}
          />
          <span>{builder?.name ?? "Builder no longer listed"}</span>
          <span className={`app-status-chip ${app.status}`}>{app.status}</span>
        </div>
        {error !== null && <p className="form-error">{error}</p>}
        {blockedUrl !== null && (
          <p className="app-open-blocked">
            {POPUP_BLOCKED_NOTE}{" "}
            <a href={blockedUrl} target="_blank" rel="noopener noreferrer">
              {app.name}
            </a>
          </p>
        )}
        <div className="member-modal-actions">
          {/* Remix is offered on any live app, your own included: a remix of
              your own app is a new app, not an edit of the one people already
              have open (stage 3 D10). */}
          {app.status === "live" && (
            <button
              className="btn"
              disabled={opening || remixing}
              title="Copy it into an app of your own and build on that"
              onClick={remix}
            >
              {remixing ? "Remixing…" : "Remix"}
            </button>
          )}
          <button
            className="btn"
            disabled={opening || app.status !== "live" || originBase === ""}
            title={
              originBase === ""
                ? "Serving is not configured on this house."
                : app.status === "live"
                  ? "Open this app in a new tab"
                  : "This app is not live yet."
            }
            onClick={open}
          >
            {opening ? "Opening…" : "Open"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---- shell ---- */


export function AppShell() {
  const channels = useChannels((s) => s.channels);
  const activeChannelId = useChannels((s) => s.activeChannelId);
  const loaded = useChannels((s) => s.loaded);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [overlay, setOverlay] = useState<Overlay>(() => overlayFromHash());
  const showSettings = overlay === "settings";
  const showPlanRoom = overlay === "planroom";
  const [membersOpen, setMembersOpen] = useState(
    () => window.innerWidth >= 1024,
  );
  const [cheatOpen, setCheatOpen] = useState(false);
  const [creatingChannel, setCreatingChannel] = useState(false);
  const [addingMembers, setAddingMembers] = useState(false);
  const [deletingChannel, setDeletingChannel] = useState(false);
  const [channelMenuOpen, setChannelMenuOpen] = useState(false);
  const [choosingApp, setChoosingApp] = useState(false);
  const [buildSessionId, setBuildSessionId] = useState<number | null>(null);
  const [appCardId, setAppCardId] = useState<string | null>(null);
  const apps = useApps((s) => s.apps);
  const builders = useApps((s) => s.builders);
  const pendingSessionId = useApps((s) => s.pendingSessionId);
  const me = useSession((s) => s.user);
  const overlayRef = useRef(overlay);
  overlayRef.current = overlay;

  // Boot: load the sidebar, open the socket, adopt a deep-linked route.
  useEffect(() => {
    const st = useChannels.getState();
    void st.refresh();
    /* The apps menu is its own list — GET /apps, not GET /channels (brief
       D9) — so it loads alongside, and resyncs alongside on reconnect (ws.ts).
       Builders come with it because every app row wears its builder's face,
       not just the chooser's cards. */
    void useApps.getState().refresh().catch(() => {});
    void useApps.getState().loadBuilders().catch(() => {});
    /* The serving gate's origin (stage 3 D10). Boot-time because the message
       feed needs it to tell an app link from any other link, and a feed that
       learned the origin late would repaint cards under the reader. */
    void useApps.getState().loadConfig();
    if (overlayFromHash() === "none") st.setActive(channelIdFromHash());
    socket.connect();
    // Route changes from outside (notification deep-links, back button).
    const onHash = () => {
      const next = overlayFromHash();
      setOverlay(next);
      if (next !== "none") return;
      const id = channelIdFromHash();
      if (id !== null) useChannels.getState().setActive(id);
    };
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
      socket.disconnect();
    };
  }, []);

  /* A Remix somewhere else in the tree (a card in the feed) asked for the
     build modal on its new session. The shell owns that modal, so the store
     is where the two meet; consuming the request clears it. */
  useEffect(() => {
    if (pendingSessionId === null) return;
    setSidebarOpen(false);
    setAppCardId(null);
    setBuildSessionId(pendingSessionId);
    useApps.getState().clearBuildModalRequest();
  }, [pendingSessionId]);

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
    if (overlayRef.current === "none") {
      writeChannelHash(activeChannelId);
      socket.sendFocus(activeChannelId);
    }
    setSidebarOpen(false);
    // Every one of these is bound to the channel we just left — including
    // when we left it because it was deleted out from under us.
    setChannelMenuOpen(false);
    setAddingMembers(false);
    setDeletingChannel(false);
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
      })
      .catch(() => {
        /* The channel can vanish between the row landing and the fetch
           answering — deleted, or our membership revoked. Both tear the
           channel down through their own path (the WS frame, or the resync
           prune); a 404/403 here is that race, not something to shout about. */
      });
  }, [activeChannelId, loaded]);

  // Overlay open/close: while a full-panel view (settings, the Plan Room) is
  // up the user is not reading the channel, so drop server-side focus (pushes
  // for it resume).
  useEffect(() => {
    if (overlay !== "none") {
      socket.sendFocus(null);
    } else {
      socket.sendFocus(useChannels.getState().activeChannelId);
    }
  }, [overlay]);

  // Window blur/focus: keep server-side focus accurate — notification
  // suppression depends on it (spec: send on EVERY blur/focus).
  useEffect(() => {
    const onBlur = () => socket.sendFocus(null);
    const onFocus = () => {
      if (overlayRef.current !== "none") return; // overlay = no channel focused
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

  const openOverlay = (next: Exclude<Overlay, "none">) => {
    setSidebarOpen(false);
    setOverlay(next);
    // replaceState, not pushState: this app deliberately never grows history
    // entries for its own navigation.
    history.replaceState(
      null,
      "",
      next === "settings" ? SETTINGS_HASH : PLANROOM_HASH,
    );
  };
  const closeOverlay = () => {
    setOverlay("none");
    writeChannelHash(useChannels.getState().activeChannelId);
  };
  const openSettings = () => openOverlay("settings");
  const openPlanRoom = () => openOverlay("planroom");

  const active = channels.find((c) => c.id === activeChannelId);
  /* app_build channels are real channels the server keeps listing — reconnect
     resync and unread bookkeeping depend on that — but they are never sidebar
     rows: their only door is the build modal. Filtered out once, before both
     groups, so neither can grow one by accident. */
  const listable = channels.filter((c) => c.type !== "app_build");
  const dms = listable.filter((c) => c.type === "dm_1to1");
  const mains = listable.filter((c) => c.type !== "dm_1to1"); // main_feed + text
  const builderFor = (app: App): Builder | undefined =>
    builders.find((b) => b.bot_id === app.builder_bot_id);
  const cardApp = apps.find((a) => a.id === appCardId);
  const select = (id: number) => {
    if (overlayRef.current !== "none") closeOverlay();
    useChannels.getState().setActive(id);
    // Same channel clicked from an overlay: the effect won't re-fire.
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
  /* Deletion is a text-channel verb only: the main feed and DMs are not
     deletable at all (the server 400s), so the item is never offered there.
     An admin may delete a private channel they are not in — the row is all
     they can see of it, and it is enough to take it away. Protected names
     (#custodian; mirrors PROTECTED_CHANNEL_NAMES in
     server/app/routers/channels.py) are refused by the server the same way,
     so the item is withheld for them too. */
  const canDelete =
    active !== undefined &&
    active.type === "text" &&
    !PROTECTED_CHANNEL_NAMES.has(active.name ?? "") &&
    (isOwner || me?.is_admin === true);
  const canAddMembers = activePrivate && activeMember && isOwner;
  const canLeave = activePrivate && activeMember;

  /* Clicking an app row: resume the live session if there is one; start a new
     one if it is my own draft (the server still rules on quota and locks —
     this only decides which verb to try); otherwise show the read-only card.
     A refusal is the server's sentence, said out loud. */
  const openApp = (app: App) => {
    setSidebarOpen(false);
    if (app.open_session !== null) {
      setBuildSessionId(app.open_session.id);
      return;
    }
    if (me !== null && app.owner_user_id === me.id && app.status === "draft") {
      useApps
        .getState()
        .startSession(app.builder_bot_id, app.id)
        .then(
          (session) => setBuildSessionId(session.id),
          (err: unknown) => {
            window.alert(
              err instanceof ApiError
                ? err.detail
                : "Failed to start the build session",
            );
          },
        );
      return;
    }
    setAppCardId(app.id);
  };

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
          {/* The Plan Room sits above the channels: it is the house's work, not
              one of its rooms. Everyone may read it — the write controls
              inside are gated, and the server's refusal is the wall. */}
          <button
            className={`channel-item plan-room-item${
              showPlanRoom ? " active" : ""
            }`}
            onClick={openPlanRoom}
          >
            <span className="hash">▤</span>
            <span className="channel-item-text">
              <span className="name">Plan Room</span>
            </span>
          </button>
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
              active={c.id === activeChannelId && overlay === "none"}
              onSelect={select}
            />
          ))}
          <div className="channel-section channel-section-row">
            <span>Apps</span>
            <button
              className="icon-btn add-channel-btn"
              title="Add or build an app"
              aria-label="Add or build an app"
              onClick={() => setChoosingApp(true)}
            >
              +
            </button>
          </div>
          {apps.length === 0 && (
            <span className="channel-section" style={{ textTransform: "none" }}>
              No apps yet
            </span>
          )}
          {apps.map((a) => (
            <AppRow
              key={a.id}
              app={a}
              builder={builderFor(a)}
              onOpen={openApp}
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
              active={c.id === activeChannelId && overlay === "none"}
              onSelect={select}
            />
          ))}
        </div>
        <UserFooter onOpenSettings={openSettings} />
      </nav>
      <main className="main-panel">
        {showPlanRoom ? (
          <PlanRoomView onClose={closeOverlay} />
        ) : showSettings ? (
          <SettingsView onClose={closeOverlay} />
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
              {(canAddMembers || canLeave || canDelete) && (
                <ChannelMenu
                  canAddMembers={canAddMembers}
                  canLeave={canLeave}
                  canDelete={canDelete}
                  open={channelMenuOpen}
                  onToggle={() => setChannelMenuOpen((v) => !v)}
                  onClose={() => setChannelMenuOpen(false)}
                  onAddMembers={() => {
                    setChannelMenuOpen(false);
                    setAddingMembers(true);
                  }}
                  onLeave={leaveActive}
                  onDelete={() => {
                    setChannelMenuOpen(false);
                    setDeletingChannel(true);
                  }}
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
      {deletingChannel && active !== undefined && canDelete && (
        <DeleteChannelModal
          channelId={active.id}
          channelName={active.name ?? ""}
          isPrivate={activePrivate}
          onClose={() => setDeletingChannel(false)}
        />
      )}
      {choosingApp && (
        <AppsChooserModal
          onStarted={(session) => {
            setChoosingApp(false);
            setBuildSessionId(session.id);
          }}
          onClose={() => setChoosingApp(false)}
        />
      )}
      {buildSessionId !== null && (
        <AppBuildModal
          sessionId={buildSessionId}
          onClose={() => setBuildSessionId(null)}
        />
      )}
      {cardApp !== undefined && (
        <AppCardModal
          app={cardApp}
          builder={builderFor(cardApp)}
          onClose={() => setAppCardId(null)}
        />
      )}
      {/* Added to / removed from a private channel — a modal for now. */}
      <MembershipNotice onOpenChannel={select} />
      {/* The channel you were reading just went away — a corner notice, not a
          dialog: nothing was done to you and there is nothing to answer. */}
      <ChannelDeletedToast />
    </div>
  );
}
