/* The build modal: a real Disjorn channel on the left, the build's own state
 * on the right (SPECS/2026-08-30-apps-tab-v1.md, stage 1).
 *
 * The chat is not a simulation of chat. The session's channel is an
 * `app_build` channel with exactly two members — you and the builder resident
 * — so MessageList and Composer are the same components the main shell uses,
 * on the same store, with the same history, typing and privacy filter. That
 * is why this file loads messages and the roster itself and hands the server
 * its focus: AppShell does those for the ACTIVE channel, and this channel is
 * never the active one.
 *
 * The preview is FROZEN and stays frozen in stage 1 (Round 5 vocabulary): a
 * bordered panel with an in-progress banner and NO iframe element at all.
 * There is nothing to point one at — the serving gate and the app origin are
 * stage 3 — and an iframe with no src would be a picture of a feature. Open,
 * Share and Change something render disabled for the same reason, each saying
 * what would enable it.
 *
 * The stage bar reads ONLY from the apps store. Nothing publishes stage
 * events yet; when the harness does, this bar moves without being touched.
 */

import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api";
import { useApps } from "../stores/apps";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { useMessages } from "../stores/messages";
import type { Attachment, Message } from "../types";
import { APP_STAGE_LABELS, APP_STAGES } from "../types";
import { socket } from "../ws";
import { QuotaMeter } from "./AppsChooserModal";
import { BotAvatar } from "./Avatar";
import { Composer } from "./Composer";
import { ImageModal } from "./ImageModal";
import { MessageList } from "./MessageList";
import { SummarizeModal } from "./SummarizeModal";

/** Lock TTL is 900s server-side; a minute keeps it alive with room to spare. */
const HEARTBEAT_MS = 60_000;
/** Mirrors D10's bound. The server is the wall; this is only courtesy. */
const NAME_MAX = 60;

function elapsedSince(startedAt: string, now: number): string {
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return "—";
  const seconds = Math.max(0, Math.floor((now - started) / 1000));
  const s = seconds % 60;
  const m = Math.floor(seconds / 60) % 60;
  const h = Math.floor(seconds / 3600);
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

export function AppBuildModal({
  sessionId,
  onClose,
}: {
  sessionId: number;
  /** Closes the modal WITHOUT ending the session — the lock TTL covers
      abandonment, and the ✕ is not a destructive verb. */
  onClose: () => void;
}) {
  const session = useApps((s) => s.sessions[sessionId]);
  const quota = useApps((s) => s.quota);

  const [loadError, setLoadError] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [ending, setEnding] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const [showPreview, setShowPreview] = useState(false);
  const [replyTo, setReplyTo] = useState<Message | null>(null);
  const [editing, setEditing] = useState<Message | null>(null);
  const [imageAtt, setImageAtt] = useState<Attachment | null>(null);
  const [summarizeTarget, setSummarizeTarget] = useState<string | null>(null);

  const channelId = session?.channel_id ?? null;
  const nestedOpen = imageAtt !== null || summarizeTarget !== null;
  const nestedOpenRef = useRef(nestedOpen);
  nestedOpenRef.current = nestedOpen;

  // Fresh state on open: the row in the sidebar knows the session exists, not
  // where it got to. This also seeds `stages` — the other half of the stage
  // bar's input, the live frames being the first.
  useEffect(() => {
    useApps
      .getState()
      .loadSession(sessionId)
      .then(
        (loaded) => setEnded(loaded.ended_at !== null),
        (err: unknown) =>
          setLoadError(
            err instanceof ApiError ? err.detail : "Failed to load the session",
          ),
      );
  }, [sessionId]);

  /* The session's channel is never the shell's active channel, so nothing
     else will load it or claim focus for it. Focus is per-connection server
     state that drives push suppression: hold it while the modal is up, and
     hand it back to the active channel on the way out. */
  useEffect(() => {
    if (channelId === null) return;
    void useMessages.getState().ensureLoaded(channelId).catch(() => {});
    void useMembers.getState().ensureLoaded(channelId);
    socket.sendFocus(channelId);
    return () => {
      socket.sendFocus(useChannels.getState().activeChannelId);
    };
  }, [channelId]);

  // Heartbeat while mounted. A 410 is final: the session ended, or its lock
  // lapsed and every reader now treats it as ended (brief D6).
  useEffect(() => {
    if (ended) return;
    const beat = () => {
      useApps
        .getState()
        .heartbeat(sessionId)
        .catch((err: unknown) => {
          if (err instanceof ApiError && err.status === 410) setEnded(true);
        });
    };
    const timer = setInterval(beat, HEARTBEAT_MS);
    return () => clearInterval(timer);
  }, [sessionId, ended]);

  // Elapsed clock. Seconds, since the session's own start — never a percent
  // and never an estimate of how far along the build is.
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  // Esc closes — unless a nested modal owns it (both ImageModal and
  // SummarizeModal listen on the window too), or the key came from a text
  // field, where Esc already means "cancel this reply/edit" in the Composer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || nestedOpenRef.current) return;
      const el = e.target;
      if (
        el instanceof HTMLElement &&
        (el.tagName === "TEXTAREA" ||
          el.tagName === "INPUT" ||
          el.isContentEditable)
      ) {
        return;
      }
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const commitRename = () => {
    setRenaming(false);
    const next = nameDraft.trim();
    if (session === undefined || next.length === 0 || next === session.app.name) {
      return;
    }
    useApps
      .getState()
      .renameApp(session.app.id, next.slice(0, NAME_MAX))
      .catch((err: unknown) => {
        setLoadError(
          err instanceof ApiError ? err.detail : "Failed to rename the app",
        );
      });
  };

  const endNow = () => {
    if (session === undefined || ending) return;
    setEnding(true);
    useApps
      .getState()
      .endSession(sessionId)
      .then(
        () => {
          setEnding(false);
          setEnded(true);
        },
        (err: unknown) => {
          setEnding(false);
          // 410 means it was already over — the end verb is idempotent, so
          // that is the outcome we asked for, not a failure.
          if (err instanceof ApiError && err.status === 410) {
            setEnded(true);
            return;
          }
          setLoadError(
            err instanceof ApiError ? err.detail : "Failed to end the session",
          );
        },
      );
  };

  if (session === undefined) {
    return (
      <div className="modal-backdrop app-build-backdrop" onClick={onClose}>
        <div
          className="app-build-modal"
          role="dialog"
          aria-modal="true"
          aria-label="App build"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="member-modal-head">
            <span className="member-modal-title">Build</span>
            <button className="icon-btn" aria-label="Close" onClick={onClose}>
              ✕
            </button>
          </div>
          <p className={loadError === null ? "member-modal-note" : "form-error"}>
            {loadError ?? "Loading the session…"}
          </p>
        </div>
      </div>
    );
  }

  const { app, builder } = session;
  const reached = session.stage === null ? -1 : APP_STAGES.indexOf(session.stage);

  /* The nested modals are SIBLINGS of the backdrop, not children of it: a
     click inside one would otherwise bubble to the backdrop's onClick and
     close the whole build modal behind it. */
  return (
    <>
    <div className="modal-backdrop app-build-backdrop" onClick={onClose}>
      <div
        className="app-build-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Building ${app.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head app-build-head">
          <BotAvatar src={builder.avatar_url} name={builder.name} size={32} />
          <span className="app-build-builder">{builder.name}</span>
          {renaming ? (
            <input
              className="app-name-input"
              autoFocus
              maxLength={NAME_MAX}
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") setRenaming(false);
              }}
            />
          ) : (
            <button
              className="app-name-button"
              title="Rename this app"
              onClick={() => {
                setNameDraft(app.name);
                setRenaming(true);
              }}
            >
              {app.name}
            </button>
          )}
          <QuotaMeter quota={quota} />
          <button
            className="btn app-build-toggle"
            aria-pressed={showPreview}
            onClick={() => setShowPreview((v) => !v)}
          >
            {showPreview ? "Chat" : "Progress"}
          </button>
          <button className="btn" disabled={ended || ending} onClick={endNow}>
            {ending ? "Ending…" : ended ? "Ended" : "End session"}
          </button>
          <button
            className="icon-btn"
            aria-label="Close"
            title="Close — the session stays open"
            onClick={onClose}
          >
            ✕
          </button>
        </div>

        {loadError !== null && <p className="form-error">{loadError}</p>}

        <div className={`app-build-body${showPreview ? " show-preview" : ""}`}>
          <div className="app-build-chat">
            <div className="app-intro-card">
              <strong>Say what you want to build.</strong> {builder.name} will
              ask what it needs, then build it. Talk for as long as you like
              first — nothing starts until it has enough.
            </div>
            <MessageList
              channelId={session.channel_id}
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
            {ended ? (
              <div className="app-build-ended">This session ended.</div>
            ) : (
              <Composer
                channelId={session.channel_id}
                channelName={app.name}
                replyTo={replyTo}
                onCancelReply={() => setReplyTo(null)}
                editing={editing}
                onStartEdit={(m) => {
                  setReplyTo(null);
                  setEditing(m);
                }}
                onCancelEdit={() => setEditing(null)}
              />
            )}
          </div>

          <div className="app-build-preview">
            <ol className="stage-bar">
              {APP_STAGES.map((stage, i) => (
                <li
                  key={stage}
                  className={`stage-step${
                    i < reached ? " done" : i === reached ? " current" : ""
                  }`}
                >
                  <span className="stage-dot" aria-hidden />
                  <span className="stage-label">{APP_STAGE_LABELS[stage]}</span>
                </li>
              ))}
            </ol>
            <div className="app-elapsed">
              <span className="app-elapsed-label">Elapsed</span>
              <span className="app-elapsed-clock">
                {elapsedSince(session.started_at, now)}
              </span>
            </div>
            <div className="app-preview-frozen">
              <div className="app-preview-banner">
                Build in progress — the preview is frozen.
              </div>
              <div className="app-preview-empty" aria-hidden />
            </div>
            <div className="app-preview-actions">
              <button
                className="btn"
                disabled
                title="Enabled when the build reaches live."
              >
                Open
              </button>
              <button
                className="btn"
                disabled
                title="Enabled when the build reaches live."
              >
                Share
              </button>
              <button
                className="btn"
                disabled
                title="Enabled when the builder idles at files written."
              >
                Change something
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

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
          channelId={session.channel_id}
          onClose={() => setSummarizeTarget(null)}
        />
      )}
    </>
  );
}
