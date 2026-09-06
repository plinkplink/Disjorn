/* The APPS group's `+` — two paths, one modal (Amendment A, edit 1).
 *
 * "Add an existing app" browses what is visible to you (public, or shared
 * with you) and puts it on your menu; "Build a new app" picks a resident
 * builder and starts a session. Public does NOT mean on-your-menu — the same
 * model channels use — which is why discover and the sidebar are two lists.
 *
 * Builder cards print the model pin the seat actually declares, or say the
 * seat declares none. There is no fallback string: a model name this client
 * invented would be a lie about what is running (provenance-printing policy).
 */

import { useEffect, useState } from "react";

import { ApiError, discoverApps } from "../api";
import { useApps } from "../stores/apps";
import type { App, AppSession, Builder, Quota } from "../types";
import { BotAvatar } from "./Avatar";

type Path = "choose" | "add" | "build";

/**
 * The daily build meter. Sessions per user per UTC day (brief D5) — not
 * tokens, not minutes: the number on screen is the number the server counts.
 */
export function QuotaMeter({ quota }: { quota: Quota | null }) {
  if (quota === null) return <div className="app-quota" />;
  const left = Math.max(0, quota.left);
  const used = Math.min(quota.used, quota.cap);
  const fraction = quota.cap > 0 ? used / quota.cap : 1;
  return (
    <div className="app-quota" title={`Resets at ${quota.resets_at}`}>
      <span className="app-quota-label">
        {left === 1 ? "1 build left today" : `${left} builds left today`}
      </span>
      <span className="app-quota-track" aria-hidden>
        <span
          className={`app-quota-fill${left === 0 ? " spent" : ""}`}
          style={{ width: `${Math.round(fraction * 100)}%` }}
        />
      </span>
    </div>
  );
}

/**
 * Mirrors the server's 429 refusal (contract table, POST /apps/sessions) so
 * the disabled cards can say WHY before anyone clicks. It is a mirror, not
 * the wall: a start that actually trips the cap shows the server's own
 * sentence verbatim, whatever it says.
 */
export function quotaExhaustedSentence(cap: number): string {
  return `You have used all ${cap} app builds for today; the meter resets at midnight UTC.`;
}

function ModelLine({ model }: { model: string | null }) {
  return (
    <span className={`builder-model${model === null ? " undeclared" : ""}`}>
      {model === null ? "model not declared by seat" : `model: ${model}`}
    </span>
  );
}

export function AppsChooserModal({
  onStarted,
  onClose,
}: {
  /** A session exists — the caller opens the build modal on it. */
  onStarted: (session: AppSession) => void;
  onClose: () => void;
}) {
  const quota = useApps((s) => s.quota);
  const builders = useApps((s) => s.builders);
  const [path, setPath] = useState<Path>("choose");
  const [discover, setDiscover] = useState<App[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | number | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The meter is on screen from the first frame, so it is loaded here rather
  // than assumed fresh from whenever the sidebar last refreshed.
  useEffect(() => {
    const st = useApps.getState();
    void st.refresh().catch(() => {});
    void st.loadBuilders().catch(() => {});
  }, []);

  useEffect(() => {
    if (path !== "add" || discover !== null) return;
    discoverApps().then(
      (list) => setDiscover(list),
      (err: unknown) => {
        setDiscover([]);
        setError(err instanceof ApiError ? err.detail : "Failed to load apps");
      },
    );
  }, [path, discover]);

  const exhausted = quota !== null && quota.left <= 0;

  const pickBuilder = (builder: Builder) => {
    if (busyId !== null || exhausted) return;
    setBusyId(builder.bot_id);
    setError(null);
    useApps
      .getState()
      .startSession(builder.bot_id)
      .then(
        (session) => {
          setBusyId(null);
          onStarted(session);
        },
        (err: unknown) => {
          setBusyId(null);
          // Includes the 429 and the 409 — the server's sentence, as written.
          setError(
            err instanceof ApiError ? err.detail : "Failed to start the build",
          );
        },
      );
  };

  const addApp = (app: App) => {
    if (busyId !== null) return;
    setBusyId(app.id);
    setError(null);
    useApps
      .getState()
      .addToMenu(app.id)
      .then(
        () => {
          setBusyId(null);
          setDiscover((prev) => (prev ?? []).filter((a) => a.id !== app.id));
        },
        (err: unknown) => {
          setBusyId(null);
          setError(
            err instanceof ApiError ? err.detail : "Failed to add the app",
          );
        },
      );
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="apps-chooser-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Apps"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head">
          <span className="member-modal-title">
            {path === "add"
              ? "Add an existing app"
              : path === "build"
                ? "Build a new app"
                : "Apps"}
          </span>
          <QuotaMeter quota={quota} />
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>

        {path === "choose" && (
          <div className="app-path-grid">
            <button className="app-path" onClick={() => setPath("add")}>
              <span className="app-path-title">Add an existing app</span>
              <span className="app-path-note">
                Apps that are public, or shared with you. Adding one puts it on
                your menu.
              </span>
            </button>
            <button
              className="app-path"
              onClick={() => setPath("build")}
              disabled={exhausted}
              title={
                exhausted && quota !== null
                  ? quotaExhaustedSentence(quota.cap)
                  : undefined
              }
            >
              <span className="app-path-title">Build a new app</span>
              <span className="app-path-note">
                Talk to a resident builder until it has enough, then it builds.
              </span>
            </button>
          </div>
        )}

        {path === "add" && (
          <div className="member-modal-body">
            {discover === null && <p className="member-modal-note">Loading…</p>}
            {discover !== null && discover.length === 0 && (
              <p className="member-modal-note">Nothing shared with you yet.</p>
            )}
            {(discover ?? []).map((app) => (
              <div className="app-discover-row" key={app.id}>
                <span className="app-discover-text">
                  <span className="app-discover-name">{app.name}</span>
                  {app.description.length > 0 && (
                    <span className="app-discover-desc">{app.description}</span>
                  )}
                </span>
                <button
                  className="btn"
                  disabled={busyId !== null}
                  onClick={() => addApp(app)}
                >
                  {busyId === app.id ? "Adding…" : "Add"}
                </button>
              </div>
            ))}
          </div>
        )}

        {path === "build" && (
          <div className="member-modal-body">
            {exhausted && quota !== null && (
              <p className="form-error">{quotaExhaustedSentence(quota.cap)}</p>
            )}
            {builders.length === 0 && (
              <p className="member-modal-note">
                No builder seats are configured on this server yet.
              </p>
            )}
            {builders.map((b) => (
              <button
                key={b.bot_id}
                className="builder-card"
                disabled={exhausted || busyId !== null}
                title={
                  exhausted && quota !== null
                    ? quotaExhaustedSentence(quota.cap)
                    : `Build with ${b.name}`
                }
                onClick={() => pickBuilder(b)}
              >
                <BotAvatar src={b.avatar_url} name={b.name} size={38} />
                <span className="builder-card-text">
                  <span className="builder-name">{b.name}</span>
                  <ModelLine model={b.model} />
                </span>
                <span className="builder-stat">
                  {b.builds_total === 1 ? "1 build" : `${b.builds_total} builds`}
                </span>
                {busyId === b.bot_id && (
                  <span className="builder-stat">Starting…</span>
                )}
              </button>
            ))}
          </div>
        )}

        {error !== null && <p className="form-error">{error}</p>}

        {path !== "choose" && (
          <div className="member-modal-actions">
            <button className="btn" onClick={() => setPath("choose")}>
              Back
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
