/* Settings (WP11) — hash #/settings. Sections:
     Profile        display-name edit + avatar upload with local preview
     Notifications  per-device Web Push enable/disable + notify_all_main pref
     Sounds         per-device in-app chimes: on/off, mentions only, volume
     Reset password admin only: hand another account a temporary password
     Deleted channels admin only: the paper trail of channel deletions
     Account        username + log out
   Push permission is requested HERE and only here (spec §10). */

import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  PASSWORD_MIN_LENGTH,
  adminResetPassword,
  getNotifyPrefs,
  listChannelDeletions,
  listUsers,
  putNotifyPrefs,
  updateMe,
  uploadAvatar,
} from "../api";
import { isIos, isStandalone, useInstall } from "../install";
import { usePush } from "../push";
import { previewSound, useSoundSettings } from "../sounds";
import { useSession } from "../stores/session";
import type { AdminUserRow, ChannelDeletion } from "../types";
import { socket } from "../ws";

/* ---------------------------------------------------------------- profile */

function ProfileSection() {
  const user = useSession((s) => s.user);
  const setUser = useSession((s) => s.setUser);

  const [name, setName] = useState(user?.display_name ?? "");
  const [nameBusy, setNameBusy] = useState(false);
  const [nameNote, setNameNote] = useState<string | null>(null);

  const fileRef = useRef<HTMLInputElement | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [avatarNote, setAvatarNote] = useState<string | null>(null);

  // Object URLs leak unless revoked.
  useEffect(() => {
    return () => {
      if (previewUrl !== null) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  if (user === null) return null;

  const saveName = async () => {
    const trimmed = name.trim();
    if (trimmed.length === 0 || trimmed === user.display_name) return;
    setNameBusy(true);
    setNameNote(null);
    try {
      const updated = await updateMe({ display_name: trimmed });
      setUser(updated);
      setNameNote("Saved");
    } catch (err) {
      setNameNote(err instanceof ApiError ? err.detail : "Save failed");
    } finally {
      setNameBusy(false);
    }
  };

  const pickFile = (file: File | null) => {
    setAvatarNote(null);
    setPendingFile(file);
    setPreviewUrl((old) => {
      if (old !== null) URL.revokeObjectURL(old);
      return file !== null ? URL.createObjectURL(file) : null;
    });
  };

  const saveAvatar = async () => {
    if (pendingFile === null) return;
    setAvatarBusy(true);
    setAvatarNote(null);
    try {
      const res = await uploadAvatar(pendingFile);
      // res.url carries the new file's `?v={mtime}`, so every <img> rendered
      // from the refreshed session user misses the stale cache entry.
      setUser({ ...user, avatar_path: res.avatar_path, avatar_url: res.url });
      pickFile(null);
      setAvatarNote("Avatar updated");
    } catch (err) {
      setAvatarNote(err instanceof ApiError ? err.detail : "Upload failed");
    } finally {
      setAvatarBusy(false);
    }
  };

  return (
    <section className="settings-section">
      <h2>Profile</h2>

      <div className="settings-avatar-row">
        <div className="avatar settings-avatar" aria-hidden>
          {user.display_name.slice(0, 1).toUpperCase()}
          {previewUrl !== null ? (
            <img src={previewUrl} alt="" />
          ) : (
            user.avatar_url != null && <img src={user.avatar_url} alt="" />
          )}
        </div>
        <div className="settings-avatar-actions">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
          />
          <button className="btn" onClick={() => fileRef.current?.click()}>
            Choose image…
          </button>
          {pendingFile !== null && (
            <>
              <button
                className="btn btn-primary"
                disabled={avatarBusy}
                onClick={() => void saveAvatar()}
              >
                {avatarBusy ? "Uploading…" : "Upload avatar"}
              </button>
              <button className="btn" onClick={() => pickFile(null)}>
                Cancel
              </button>
            </>
          )}
          {avatarNote !== null && <span className="settings-note">{avatarNote}</span>}
        </div>
      </div>

      <div className="field">
        <label htmlFor="settings-display-name">Display name</label>
        <div className="settings-inline">
          <input
            id="settings-display-name"
            value={name}
            maxLength={80}
            onChange={(e) => {
              setName(e.target.value);
              setNameNote(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") void saveName();
            }}
          />
          <button
            className="btn btn-primary"
            disabled={
              nameBusy || name.trim().length === 0 || name.trim() === user.display_name
            }
            onClick={() => void saveName()}
          >
            {nameBusy ? "Saving…" : "Save"}
          </button>
        </div>
        {nameNote !== null && <span className="settings-note">{nameNote}</span>}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------- notifications */

function PushControls() {
  const { status, detail, busy, enable, disable } = usePush();

  switch (status) {
    case "checking":
      return <p className="settings-note">Checking push state…</p>;
    case "unsupported":
      return (
        <p className="settings-note">
          This browser does not support Web Push notifications.
          {isIos() && !isStandalone() && (
            <span className="settings-hint">
              On iOS, install Disjorn first: open the Share menu and choose
              “Add to Home Screen”, then enable notifications from inside the
              installed app.
            </span>
          )}
        </p>
      );
    case "not-configured":
      return (
        <p className="settings-note">
          Push is not configured on the server.
          {detail !== null && <span className="settings-hint"> {detail}</span>}
        </p>
      );
    case "blocked":
      return (
        <p className="settings-note">
          Notifications are blocked by the browser for this site. Allow them in
          your browser's site settings, then come back here.
        </p>
      );
    case "enabled":
      return (
        <div className="settings-inline">
          <span className="settings-state on">Enabled on this device</span>
          <button className="btn" disabled={busy} onClick={() => void disable()}>
            {busy ? "Working…" : "Disable"}
          </button>
        </div>
      );
    case "disabled":
      return (
        <div className="settings-inline">
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void enable()}
          >
            {busy ? "Working…" : "Enable notifications on this device"}
          </button>
          {detail !== null && <span className="settings-note">{detail}</span>}
        </div>
      );
    case "error":
      return (
        <div className="settings-inline">
          <span className="settings-note">{detail ?? "Something went wrong"}</span>
          <button className="btn" disabled={busy} onClick={() => void enable()}>
            Retry
          </button>
        </div>
      );
  }
}

function NotificationsSection() {
  const refreshPush = usePush((s) => s.refresh);
  const [pref, setPref] = useState<boolean | null>(null); // null = loading/unavailable
  const [prefNote, setPrefNote] = useState<string | null>(null);

  useEffect(() => {
    void refreshPush();
    getNotifyPrefs()
      .then((p) => setPref(p.notify_all_main))
      .catch(() => setPrefNote("Could not load notification preferences"));
  }, [refreshPush]);

  const togglePref = (next: boolean) => {
    const prev = pref;
    setPref(next); // optimistic
    setPrefNote(null);
    putNotifyPrefs({ notify_all_main: next }).catch(() => {
      setPref(prev);
      setPrefNote("Could not save the preference");
    });
  };

  return (
    <section className="settings-section">
      <h2>Notifications</h2>
      <PushControls />
      <label className="settings-toggle">
        <input
          type="checkbox"
          checked={pref === true}
          disabled={pref === null}
          onChange={(e) => togglePref(e.target.checked)}
        />
        <span>
          Notify me for every #main message
          <span className="settings-hint">
            Off: only DMs and @mentions push. Applies to all your devices.
          </span>
        </span>
      </label>
      {prefNote !== null && <span className="settings-note">{prefNote}</span>}
    </section>
  );
}

/* ----------------------------------------------------------------- sounds */

function SoundsSection() {
  const enabled = useSoundSettings((s) => s.enabled);
  const mentionsOnly = useSoundSettings((s) => s.mentionsOnly);
  const volume = useSoundSettings((s) => s.volume);
  const update = useSoundSettings((s) => s.update);
  const percent = Math.round(volume * 100);

  return (
    <section className="settings-section">
      <h2>Sounds</h2>
      <label className="settings-toggle">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => update({ enabled: e.target.checked })}
        />
        <span>
          Play a sound when a message arrives
          <span className="settings-hint">
            A soft chime for unread messages, a brighter one for DMs and
            @mentions of you. With push notifications on for this device,
            chimes play only while the Disjorn window is focused; the
            notification alerts you otherwise. This device only.
          </span>
          <span className="settings-hint">
            Turning sounds off does not silence push notifications. Those
            follow the Notifications setting above and your system settings.
          </span>
        </span>
      </label>
      <label className="settings-toggle">
        <input
          type="checkbox"
          checked={mentionsOnly}
          disabled={!enabled}
          onChange={(e) => update({ mentionsOnly: e.target.checked })}
        />
        <span>Only for DMs and @mentions</span>
      </label>
      <div className="field">
        <label htmlFor="settings-sound-volume">Volume</label>
        <div className="settings-inline">
          <input
            id="settings-sound-volume"
            type="range"
            min={0}
            max={100}
            step={5}
            value={percent}
            disabled={!enabled}
            aria-valuetext={`${percent}%`}
            onChange={(e) => update({ volume: Number(e.target.value) / 100 })}
          />
          <span className="settings-note settings-volume-value" aria-hidden>
            {percent}%
          </span>
          <button
            type="button"
            className="btn"
            disabled={!enabled}
            onClick={() => previewSound("message", volume)}
          >
            Test message
          </button>
          <button
            type="button"
            className="btn"
            disabled={!enabled}
            onClick={() => previewSound("mention", volume)}
          >
            Test mention
          </button>
        </div>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------- app */

/** Install hint (WP12): renders only when there is something to say —
    a captured beforeinstallprompt (Android/desktop Chrome) or a fresh
    install confirmation. Standalone launches show nothing. The iOS
    Add-to-Home-Screen note lives under the push `unsupported` state. */
function InstallSection() {
  const canPrompt = useInstall((s) => s.canPrompt);
  const installed = useInstall((s) => s.installed);

  if (isStandalone() || (!canPrompt && !installed)) return null;

  return (
    <section className="settings-section">
      <h2>App</h2>
      {installed ? (
        <p className="settings-note">
          Installed — launch Disjorn from your home screen or app list.
        </p>
      ) : (
        <div className="settings-inline">
          <button
            className="btn btn-primary"
            onClick={() => void useInstall.getState().promptInstall()}
          >
            Install app
          </button>
          <span className="settings-hint">
            Adds Disjorn to your home screen and opens it in its own window.
          </span>
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------- reset password */

/** A handover password nobody has to invent: 16 url-safe chars from the CSPRNG. */
function randomHandover(): string {
  const alphabet =
    "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789";
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

/**
 * The admin half of "I forgot my password". The server route is narrow on
 * purpose (see admin_reset_password in routers/auth.py): it sets a password
 * the account must replace on first login and ends every session it had.
 * This form is the only client surface for it, and it renders for admins only.
 */
function ResetPasswordSection() {
  const me = useSession((s) => s.user);
  const [users, setUsers] = useState<AdminUserRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [targetId, setTargetId] = useState<number | null>(null);
  const [handover, setHandover] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ username: string; password: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    listUsers()
      .then((rows) => {
        if (!cancelled) setUsers(rows);
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(err instanceof ApiError ? err.detail : "Could not load accounts");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (me === null || !me.is_admin) return null;

  // You reset your own password on the change-password screen; the server
  // answers 400 to a self-reset, so the option is not offered at all.
  const others = (users ?? []).filter((u) => u.id !== me.id);
  const target = others.find((u) => u.id === targetId) ?? null;
  const tooShort = handover.length > 0 && handover.length < PASSWORD_MIN_LENGTH;
  const ready = target !== null && handover.length >= PASSWORD_MIN_LENGTH && !busy;

  const submit = async () => {
    if (!ready || target === null) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      await adminResetPassword(target.id, handover);
      setDone({ username: target.username, password: handover });
      setUsers((rows) =>
        rows === null
          ? rows
          : rows.map((u) => (u.id === target.id ? { ...u, must_change_password: true } : u)),
      );
      setHandover("");
      setTargetId(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Reset failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="settings-section">
      <h2>Reset a password</h2>
      <p className="settings-note">
        For someone who is locked out. They log in once with the password you
        give them, then must choose their own before anything else works. All
        of their devices are signed out.
      </p>

      {loadError !== null && <p className="form-error">{loadError}</p>}

      <div className="field">
        <label htmlFor="reset-user">Account</label>
        <select
          id="reset-user"
          value={targetId ?? ""}
          disabled={users === null || busy}
          onChange={(e) => {
            setTargetId(e.target.value === "" ? null : Number(e.target.value));
            setDone(null);
            setError(null);
          }}
        >
          <option value="">
            {users === null ? "Loading…" : "Choose an account…"}
          </option>
          {others.map((u) => (
            <option key={u.id} value={u.id}>
              {u.display_name} ({u.username})
              {u.must_change_password ? " — reset pending" : ""}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="reset-password">Temporary password</label>
        <div className="settings-inline">
          <input
            id="reset-password"
            type="text"
            value={handover}
            onChange={(e) => setHandover(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            aria-describedby="reset-password-hint"
          />
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => setHandover(randomHandover())}
          >
            Generate
          </button>
        </div>
        <p className="field-hint" id="reset-password-hint">
          At least {PASSWORD_MIN_LENGTH} characters. Shown in the clear so you
          can pass it on; it stops working the moment they replace it.
        </p>
      </div>

      {tooShort && (
        <p className="form-error">
          That is {handover.length} character{handover.length === 1 ? "" : "s"} — it
          needs {PASSWORD_MIN_LENGTH}.
        </p>
      )}
      {error !== null && <p className="form-error">{error}</p>}

      <button
        type="button"
        className="btn btn-danger settings-reset-btn"
        disabled={!ready}
        onClick={() => void submit()}
      >
        {busy ? "Resetting…" : target === null ? "Reset password" : `Reset ${target.username}'s password`}
      </button>

      {done !== null && (
        <p className="settings-note settings-reset-done" role="status">
          Done. Tell <strong>{done.username}</strong> to log in with{" "}
          <code>{done.password}</code> and pick a new password when asked.
        </p>
      )}
    </section>
  );
}

/* ------------------------------------------------------ deleted channels */

const DELETIONS_PAGE = 25;

function deletionTitle(d: ChannelDeletion): string {
  switch (d.channel_type) {
    case "text":
      return `#${d.channel_name ?? "unnamed"}`;
    case "dm_1to1":
      return "Direct message";
    case "app_build":
      return d.channel_name ?? "App build chat";
    case "main_feed":
      return "#main";
  }
}

function deletionKind(d: ChannelDeletion): string {
  if (d.channel_type === "dm_1to1") return "DM";
  if (d.channel_type === "app_build") return "app build";
  return d.visibility === "private" ? "private channel" : "public channel";
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

/** Admin only: which channels were deleted, by whom and when. No contents exist to show. */
function DeletedChannelsSection() {
  const [rows, setRows] = useState<ChannelDeletion[] | null>(null);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (beforeId?: number) => {
    setBusy(true);
    setError(null);
    try {
      const page = await listChannelDeletions(beforeId, DELETIONS_PAGE);
      setRows((prev) => (beforeId === undefined || prev === null ? page : [...prev, ...page]));
      setMore(page.length === DELETIONS_PAGE);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load deleted channels");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  return (
    <section className="settings-section">
      <h2>Deleted channels</h2>
      {error !== null && <p className="form-error">{error}</p>}
      {rows === null && error === null && <p className="settings-note">Loading…</p>}
      {rows !== null && rows.length === 0 && (
        <p className="settings-note">No channel has been deleted.</p>
      )}
      {rows !== null && rows.length > 0 && (
        <ul className="settings-deletions">
          {rows.map((d) => (
            <li key={d.id} className="settings-deletion">
              <div className="settings-deletion-head">
                <strong className="settings-deletion-name">{deletionTitle(d)}</strong>
                <span className="settings-hint">{deletionKind(d)}</span>
              </div>
              <span className="settings-hint">
                {plural(d.message_count, "message")} · {plural(d.member_count, "member")}
              </span>
              <span className="settings-hint">
                Deleted by {d.deleted_by_name ?? `${d.deleted_by_type} ${d.deleted_by_id}`} ·{" "}
                <time dateTime={d.deleted_at}>{new Date(d.deleted_at).toLocaleString()}</time>
              </span>
            </li>
          ))}
        </ul>
      )}
      {more && rows !== null && (
        <button
          type="button"
          className="btn settings-reset-btn"
          disabled={busy}
          onClick={() => {
            const oldest = rows[rows.length - 1];
            if (oldest !== undefined) void load(oldest.id);
          }}
        >
          {busy ? "Loading…" : "Show older"}
        </button>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ view */

export function SettingsView({ onClose }: { onClose: () => void }) {
  const user = useSession((s) => s.user);
  const logout = useSession((s) => s.logout);

  return (
    <div className="settings-view">
      <header className="settings-head">
        <button className="icon-btn" aria-label="Back to chat" onClick={onClose}>
          ←
        </button>
        <h1>Settings</h1>
      </header>
      <div className="settings-body">
        <ProfileSection />
        <NotificationsSection />
        <SoundsSection />
        <InstallSection />
        <ResetPasswordSection />
        {user?.is_admin === true && <DeletedChannelsSection />}
        <section className="settings-section">
          <h2>Account</h2>
          {user !== null && (
            <p className="settings-note">
              Signed in as <strong>{user.username}</strong>
            </p>
          )}
          <button
            className="btn settings-logout"
            onClick={() => {
              socket.disconnect();
              void logout();
            }}
          >
            Log out
          </button>
        </section>
      </div>
    </div>
  );
}
