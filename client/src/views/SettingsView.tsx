/* Settings (WP11) — hash #/settings. Sections:
     Profile        display-name edit + avatar upload with local preview
     Notifications  per-device Web Push enable/disable + notify_all_main pref
     Reset password admin only: hand another account a temporary password
     Account        username + log out
   Push permission is requested HERE and only here (spec §10). */

import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  PASSWORD_MIN_LENGTH,
  adminResetPassword,
  getNotifyPrefs,
  listUsers,
  putNotifyPrefs,
  updateMe,
  uploadAvatar,
} from "../api";
import { isIos, isStandalone, useInstall } from "../install";
import { usePush } from "../push";
import { useSession } from "../stores/session";
import type { AdminUserRow } from "../types";
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
        <InstallSection />
        <ResetPasswordSection />
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
