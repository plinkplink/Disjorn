/* Settings (WP11) — hash #/settings. Three sections:
     Profile        display-name edit + avatar upload with local preview
     Notifications  per-device Web Push enable/disable + notify_all_main pref
     Account        username + log out
   Push permission is requested HERE and only here (spec §10). */

import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  avatarUrl,
  bumpAvatarVersion,
  getNotifyPrefs,
  putNotifyPrefs,
  updateMe,
  uploadAvatar,
} from "../api";
import { isIos, isStandalone, useInstall } from "../install";
import { usePush } from "../push";
import { useSession } from "../stores/session";
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
      bumpAvatarVersion(); // future <img> renders bypass the stale cache entry
      setUser({ ...user, avatar_path: res.avatar_path });
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
            user.avatar_path !== null && <img src={avatarUrl(user.id)} alt="" />
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
