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
 * The preview is a REAL iframe of the app's preview root, served by the gate
 * (SPECS/2026-09-09-apps-serving-gate.md, D10). Round 5's vocabulary is the
 * behaviour: while a turn runs the frame is FROZEN — banner, overlay, no
 * pointer events — and when the builder idles it THAWS and the app is the
 * user's to click. It reloads when a turn reaches `deployed`, and never at
 * any other moment: a frame that reloaded mid-turn would show half a build.
 *
 * `src` is minted per open (`POST /apps/{id}/open?root=preview`) and never
 * stored: the grant in it is short-lived by design, and the client's job is
 * to ask again, not to keep one. On a house with no gate configured
 * (`origin_base` empty, or a 503) the panel keeps stage 1's placeholder and
 * says so — a framed blank would read as a broken app rather than an absent
 * gate.
 *
 * The stage bar reads ONLY from the apps store, and it reads the DETAIL, not
 * the stage name (SPECS/2026-09-06-apps-builder-seat.md §A). `files_written`
 * is the turn's terminal stage rather than literally "files were written": a
 * turn that answered and changed nothing arrives there with `no_changes`, and
 * a halted turn arrives at whatever stage it last reached with `halted` set.
 * A bar that labelled itself from the stage word would tell the user a turn
 * that failed at `scoped` was still being scoped.
 */

import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api";
import { POPUP_BLOCKED_NOTE, openMinted } from "../lib/openMinted";
import { deployedTurnCount, isTurnRunning, useApps } from "../stores/apps";
import { useChannels } from "../stores/channels";
import { useMembers } from "../stores/members";
import { useMessages } from "../stores/messages";
import type {
  AppVisibility,
  Attachment,
  HaltReason,
  Message,
} from "../types";
import { APP_STAGE_LABELS, APP_STAGES, isChannelMember } from "../types";
import { socket } from "../ws";
import { QuotaMeter } from "./AppsChooserModal";
import { BotAvatar } from "./Avatar";
import { Composer } from "./Composer";
import { ImageModal } from "./ImageModal";
import { MessageList } from "./MessageList";
import { SummarizeModal } from "./SummarizeModal";
import { TypingLine } from "./TypingLine";

/** Lock TTL is 900s server-side; a minute keeps it alive with room to spare. */
const HEARTBEAT_MS = 60_000;
/** Mirrors D10's bound. The server is the wall; this is only courtesy. */
const NAME_MAX = 60;
/**
 * How long the send-freeze may sit in silence before the banner offers a way
 * out by hand (Claudette #2555).
 *
 * NOT a timeout: nothing here thaws on its own. Round 5's rule is that nothing
 * changes under the mouse, and a frame that unfroze itself a minute after a
 * send would be exactly that. This only decides when a BUTTON appears, and the
 * user decides whether to press it.
 */
const PENDING_SILENCE_MS = 60_000;

/* What a halt says on the chip. Short forms of the sentences the server
   already wrote into the room — the room is where the detail lives, and the
   bar is where you glance. */
const HALT_CHIP_TEXT: Record<HaltReason, string> = {
  ceiling: "hit its ceiling",
  timeout: "timed out",
  error: "failed",
  secret: "closed: credential",
  stopped: "stopped by you",
};

/* The dialog's words ARE the promise (spec: "Its words are the promise,
   exactly"). Nothing here says instant: the hard bound is the unit's stop
   timeout, and "about a minute and a half" is that number said plainly. */
const STOP_PROMISE =
  "Stop this turn? The builder gets a moment to save what it has. Files " +
  "written so far stay in the project; the preview does not change. This " +
  "ends the turn, not the session. Takes up to about a minute and a half.";
const END_STOPS_FIRST = "Ending the session stops the turn first.";

/** How many files the turn actually touched.

    The publisher caps the list at 40 names and appends a literal `+N more`,
    so the count is the names present plus whatever that marker claims. A file
    genuinely called `+3 more` would be miscounted by one line, which is the
    right trade against making every long turn read "41 files". */
function countFiles(files: string[]): number {
  const last = files[files.length - 1];
  const more = last === undefined ? null : /^\+(\d+) more$/.exec(last);
  return more === null ? files.length : files.length - 1 + Number(more[1]);
}

/** Said wherever a URL would have gone on a house with no serving gate. One
    sentence, and it names the house rather than the app: nothing is wrong
    with this app. */
const NO_GATE = "Serving is not configured on this house.";

/**
 * The share dialog: one channel, one visibility.
 *
 * The picker lists the channels the user is IN, minus `app_build` ones —
 * every build room is a two-member channel between one user and one builder,
 * and sharing an app into the room it was built in is a message to nobody.
 * The server re-checks membership; this only avoids offering a 403.
 */
function ShareDialog({
  appName,
  visibility,
  busy,
  error,
  onShare,
  onClose,
}: {
  appName: string;
  /** The app's visibility RIGHT NOW. The checkbox is seeded from it and, when
      it is already `public`, locked — see below. */
  visibility: AppVisibility;
  busy: boolean;
  error: string | null;
  /** `widenToPublic` is a request, not a setting: false means "say nothing
      about visibility", never "make it shared again". */
  onShare: (channelId: number, widenToPublic: boolean) => void;
  onClose: () => void;
}) {
  const channels = useChannels((s) => s.channels);
  const options = channels.filter(
    (c) => c.type !== "app_build" && isChannelMember(c),
  );
  const [channelId, setChannelId] = useState<number | null>(
    options[0]?.id ?? null,
  );
  /* Sharing only ever WIDENS on the server — there is no narrowing verb — so a
     box that could be unchecked on an already-public app was a control that
     did nothing, silently (Claudette #2555). Seeded from the app, and locked
     on when the app is already public: the honest reading of "you cannot take
     this back here" is a checked box you cannot clear, with the reason under
     it. */
  const alreadyPublic = visibility === "public";
  const [isPublic, setIsPublic] = useState(alreadyPublic);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="delete-channel-modal app-build-confirm"
        role="dialog"
        aria-modal="true"
        aria-label={`Share ${appName}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head">
          <span className="member-modal-title">Share {appName}</span>
        </div>
        <p className="member-modal-note">
          Everyone in the channel can open it. Public means anyone in the
          house can, whether they are in that channel or not.
        </p>
        <label className="app-share-field">
          <span className="app-share-label">Channel</span>
          <select
            className="app-share-select"
            value={channelId ?? ""}
            onChange={(e) => setChannelId(Number(e.target.value))}
          >
            {options.map((c) => (
              <option key={c.id} value={c.id}>
                {c.type === "dm_1to1"
                  ? `@${c.name ?? "Direct message"}`
                  : `#${c.name ?? c.id}`}
              </option>
            ))}
          </select>
        </label>
        <label className="app-share-check">
          <input
            type="checkbox"
            checked={isPublic}
            disabled={alreadyPublic}
            onChange={(e) => setIsPublic(e.target.checked)}
          />
          <span>Public — anyone in the house can open it</span>
        </label>
        {alreadyPublic && (
          <p className="app-share-locked">
            already public — sharing can only widen
          </p>
        )}
        {error !== null && <p className="form-error">{error}</p>}
        <div className="member-modal-actions">
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={busy || channelId === null}
            onClick={() => {
              /* Only a real widening is sent. An app that is already public
                 gets no `visibility` at all — the field would be a no-op the
                 server would have to interpret. */
              if (channelId !== null) {
                onShare(channelId, isPublic && !alreadyPublic);
              }
            }}
          >
            {busy ? "Sharing…" : "Share"}
          </button>
        </div>
      </div>
    </div>
  );
}

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
  /* Stop (slice (iv)). `stopping` holds from the click until the turn's
     terminal event lands — cleared by the store deriving `done`, never by a
     timer, because the server's record is the only thing that says the turn
     ended. `confirming` is which verb the open dialog is for. */
  const [stopping, setStopping] = useState(false);
  const [confirming, setConfirming] = useState<"stop" | "end" | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const [showPreview, setShowPreview] = useState(false);
  const [replyTo, setReplyTo] = useState<Message | null>(null);
  const [editing, setEditing] = useState<Message | null>(null);
  const [imageAtt, setImageAtt] = useState<Attachment | null>(null);
  const [summarizeTarget, setSummarizeTarget] = useState<string | null>(null);

  /* ---- stage 3: the frame, the gate, and the four verbs ---- */
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  /** Why there is no frame, in the server's words or the house's. Null when
      there is one. */
  const [previewNote, setPreviewNote] = useState<string | null>(null);
  /**
   * A message of mine is out and the room has not answered yet.
   *
   * The freeze starts HERE, not at the first stage event: Round 5's rule is
   * that nothing changes under the mouse, and a build that begins two seconds
   * after the send would otherwise repaint a frame the user was clicking. It
   * ends on the room's own answer — a stage event (a turn started; `running`
   * takes it from here) or the builder's reply (it answered in chat and no
   * turn is coming). No timer: a timer would thaw a frame while a build was
   * still spinning up, which is the exact thing being prevented.
   *
   * A builder that dies between the send and its first `scoped` sends neither
   * answer, and until Claudette #2555 that left the panel frozen with Live and
   * Change-something disabled and no way out but closing the modal. `at` is
   * when the send went out, and after PENDING_SILENCE_MS the banner grows a
   * button that clears this by hand. Still no timer that thaws anything: the
   * clock decides when to OFFER, the user decides whether to take it.
   */
  const [pendingSend, setPendingSend] = useState<{
    afterSeq: number;
    afterStages: number;
    at: number;
  } | null>(null);
  /** How long the current send-freeze has been silent, in ms. Feeds the banner
      text and the unfreeze button and NOTHING else — `frozen` never reads it,
      so no interval of this component's can change what is under the mouse. */
  const [pendingWaited, setPendingWaited] = useState(0);
  const [focusNonce, setFocusNonce] = useState(0);
  const [busy, setBusy] = useState<"live" | "open" | "share" | "revert" | null>(
    null,
  );
  /** One line under the buttons after a verb succeeded. */
  const [notice, setNotice] = useState<string | null>(null);
  /** The minted live URL when the browser refused a tab for it — offered as a
      link beside the buttons, which the user's own click can follow. */
  const [blockedUrl, setBlockedUrl] = useState<string | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  const [revertArmed, setRevertArmed] = useState(false);

  const originBase = useApps((s) => s.originBase);
  const appId = session?.app.id ?? null;
  const card = useApps((s) => (appId === null ? undefined : s.cards[appId]));

  const channelId = session?.channel_id ?? null;
  /* The turn stopped waiting on anything (it halted, or it changed nothing),
     so the clock is a final reading rather than a counter. Read up here
     because the timer effect below runs before the render body. */
  const clockFrozen = session?.lastTurn?.done === true;
  const nestedOpen = imageAtt !== null || summarizeTarget !== null;
  const nestedOpenRef = useRef(nestedOpen);
  nestedOpenRef.current = nestedOpen;
  /** Esc belongs to the share dialog while it is up — closing the whole build
      modal out from under a half-filled dialog is not what that key means. */
  const shareOpenRef = useRef(false);
  shareOpenRef.current = shareOpen;

  /* The frame's whole input. `running` is derived from the same two things
     the stage bar renders — the session's stage and the folded turn — by the
     store's own helper; there is no second "is it building" flag anywhere. */
  const running = isTurnRunning(session);
  const frozen = running || pendingSend !== null;
  /** The send-freeze is the ONLY thing holding the frame, and the room has
      said nothing for a minute. The banner offers a hand-crank; nothing here
      pulls it. */
  const pendingStale =
    pendingSend !== null && !running && pendingWaited >= PENDING_SILENCE_MS;
  const stageCount = session?.stages.length ?? 0;
  /** How many turns have DEPLOYED: the reload trigger, and Live's
      precondition. One rule, read twice — see the store. */
  const deployedCount = deployedTurnCount(session);
  /** The newest message in the build room, for the send-freeze's other exit. */
  const lastMessage = useMessages((s) => {
    if (channelId === null) return undefined;
    const list = s.byChannel[channelId]?.list;
    return list === undefined ? undefined : list[list.length - 1];
  });

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
  // and never an estimate of how far along the build is. It genuinely STOPS
  // when the turn does; a frozen reading kept alive by a live timer is a
  // clock that only looks stopped.
  useEffect(() => {
    if (clockFrozen) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [clockFrozen]);

  // Esc closes — unless a nested modal owns it (both ImageModal and
  // SummarizeModal listen on the window too), or the key came from a text
  // field, where Esc already means "cancel this reply/edit" in the Composer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || nestedOpenRef.current) return;
      if (shareOpenRef.current) {
        setShareOpen(false);
        return;
      }
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

  /* The send-freeze's exits, both of them facts from the server: a stage
     event landed (the turn started), or the builder posted (it answered in
     chat). Ending the session is a third — there is nothing left to wait for. */
  useEffect(() => {
    if (pendingSend === null) return;
    const stageLanded = stageCount > pendingSend.afterStages;
    const builderReplied =
      lastMessage !== undefined &&
      lastMessage.author_type === "bot" &&
      lastMessage.seq > pendingSend.afterSeq;
    if (ended || stageLanded || builderReplied) setPendingSend(null);
  }, [pendingSend, stageCount, lastMessage, ended]);

  /* The silence clock. Its only consumer is the banner, which is why it is a
     separate reading from the elapsed clock above: that one stops when a turn
     does, and this one is counting the absence of a turn. */
  useEffect(() => {
    if (pendingSend === null) {
      setPendingWaited(0);
      return;
    }
    const { at } = pendingSend;
    setPendingWaited(Date.now() - at);
    const timer = setInterval(() => setPendingWaited(Date.now() - at), 1000);
    return () => clearInterval(timer);
  }, [pendingSend]);

  /* The frame's src. Minted per open and re-minted on every `deployed`, which
     is also what reloads the iframe: the URL carries a fresh grant, and the
     `key` below guarantees the swap is a load and not a no-op. Re-minting is
     cheap and the alternative — reusing a grant we cached — is a client
     holding a credential it has no reason to hold. */
  useEffect(() => {
    if (appId === null) return;
    if (originBase === "") {
      setPreviewUrl(null);
      setPreviewNote(NO_GATE);
      return;
    }
    let cancelled = false;
    useApps
      .getState()
      .openApp(appId, "preview")
      .then(
        (url) => {
          if (cancelled) return;
          setPreviewUrl(url);
          setPreviewNote(null);
        },
        (err: unknown) => {
          if (cancelled) return;
          setPreviewUrl(null);
          // 503 is the gate saying it is not configured; everything else is
          // the server's own sentence, which is better than one invented here.
          setPreviewNote(
            err instanceof ApiError
              ? err.status === 503
                ? NO_GATE
                : err.detail
              : "The preview could not be opened.",
          );
        },
      );
    return () => {
      cancelled = true;
    };
  }, [appId, originBase, deployedCount]);

  /* The card is where `has_previous_live` lives — Revert is shown only when
     there is a previous live to go back to, and only the server knows. */
  useEffect(() => {
    if (appId === null) return;
    void useApps.getState().loadCard(appId);
  }, [appId]);

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

  /* A turn is running from its `scoped` until its terminal event: the store
     folds the events and `done` is that terminal fact. Idle sessions (no
     turn yet, or the last one finished) show no Stop.

     This is NOT the frame's `running` above, and the difference is the
     question each answers. Stop asks "is there a turn the server could still
     stop", which a turn that has written files but not halted still is. The
     frame asks "will anything change under the mouse", which D10 answers with
     files_written / deployed / halted. Two questions, one event stream. */
  const turnRunning =
    session?.lastTurn !== null &&
    session?.lastTurn !== undefined &&
    session.lastTurn.done !== true;

  const stopNow = () => {
    if (session === undefined || stopping) return;
    setConfirming(null);
    setStopping(true);
    useApps
      .getState()
      .stopTurn(sessionId)
      .catch((err: unknown) => {
        // 409: nothing was running by the time the click landed — the turn
        // ended on its own, which is the outcome asked for. 410: the session
        // is over, same answer. Anything else is a failure to say.
        if (err instanceof ApiError && (err.status === 409 || err.status === 410)) {
          setStopping(false);
          if (err.status === 410) setEnded(true);
          return;
        }
        setStopping(false);
        setLoadError(
          err instanceof ApiError ? err.detail : "Failed to stop the turn",
        );
      });
  };

  /* The store clears `stopping` for us: the turn's terminal event flips
     `done`, and a button that stayed "Stopping…" past that would be promising
     something that already happened. */
  useEffect(() => {
    if (stopping && !turnRunning) setStopping(false);
  }, [stopping, turnRunning]);

  const endNow = () => {
    if (session === undefined || ending) return;
    setConfirming(null);
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

  /* ---- the four verbs (D7). Each one names its own failure: the server's
     sentence when there is one, because it knows why better than this file
     does. ---- */

  const sayError = (err: unknown, fallback: string) => {
    setLoadError(err instanceof ApiError ? err.detail : fallback);
  };

  const goLiveNow = () => {
    if (busy !== null) return;
    setBusy("live");
    setLoadError(null);
    setNotice(null);
    setBlockedUrl(null);
    useApps
      .getState()
      .goLive(sessionId)
      .then(
        () => {
          setBusy(null);
          setNotice("This app is live.");
        },
        (err: unknown) => {
          setBusy(null);
          sayError(err, "Failed to publish the app");
        },
      );
  };

  /* The tab is claimed on the click, inside this handler, and navigated when
     the mint lands (`lib/openMinted`). Opening it from the promise callback —
     what this did — is outside the user-gesture task, where Safari, Firefox
     and installed PWAs hand back null and nothing happens (Claudette #2555). */
  const openLive = () => {
    if (busy !== null || appId === null) return;
    setBusy("open");
    setLoadError(null);
    setBlockedUrl(null);
    void openMinted(() => useApps.getState().openApp(appId, "live")).then(
      (result) => {
        setBusy(null);
        if (result.kind === "blocked") setBlockedUrl(result.url);
        if (result.kind === "failed") {
          sayError(result.error, "Failed to open the app");
        }
      },
    );
  };

  /* `widenToPublic` false means the field is OMITTED, not set to `shared`.
     The server only ever widens visibility, so sending `shared` for an app
     that is already public asks for something that cannot happen, and sending
     it for a private app is the server's own default anyway (Claudette
     #2555). */
  const shareNow = (targetChannelId: number, widenToPublic: boolean) => {
    if (busy !== null || appId === null) return;
    setBusy("share");
    setShareError(null);
    useApps
      .getState()
      .shareApp(appId, targetChannelId, widenToPublic ? "public" : undefined)
      .then(
        () => {
          setBusy(null);
          setShareOpen(false);
          const target = useChannels
            .getState()
            .channels.find((c) => c.id === targetChannelId);
          setNotice(
            target === undefined || target.name === null
              ? "Shared."
              : `Shared into ${target.type === "dm_1to1" ? "@" : "#"}${target.name}.`,
          );
        },
        (err: unknown) => {
          setBusy(null);
          setShareError(
            err instanceof ApiError ? err.detail : "Failed to share the app",
          );
        },
      );
  };

  const revertNow = () => {
    if (busy !== null || appId === null) return;
    setRevertArmed(false);
    setBusy("revert");
    setLoadError(null);
    setBlockedUrl(null);
    useApps
      .getState()
      .revertApp(appId)
      .then(
        () => {
          setBusy(null);
          setNotice("Reverted to the previous live version.");
        },
        (err: unknown) => {
          setBusy(null);
          sayError(err, "Failed to revert the app");
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

  /* The turn, and what it makes the bar say. `lastTurn` is derived in the
     store from the whole event list, so a new turn's `scoped` clears a halt
     and nothing else does. */
  const turn = session.lastTurn ?? null;
  const halted = turn?.halted ?? null;
  const currentLabel =
    halted !== null
      ? "Halted"
      : turn !== null && turn.no_changes && session.stage === "files_written"
        ? "No changes"
        : null;

  /* The final reading is the terminal event's own timestamp, not whenever
     this component last noticed — a modal opened ten minutes after a build
     halted must show when it halted. */
  const finishedAt = clockFrozen
    ? (session.stages[session.stages.length - 1]?.created_at ?? null)
    : null;
  const clockAt = finishedAt === null ? now : Date.parse(finishedAt) || now;

  /* The nested modals are SIBLINGS of the backdrop, not children of it: a
     click inside one would otherwise bubble to the backdrop's onClick and
     close the whole build modal behind it. */
  return (
    <>
    {confirming !== null && (
      <div
        className="modal-backdrop"
        onClick={() => setConfirming(null)}
      >
        <div
          className="delete-channel-modal app-build-confirm"
          role="alertdialog"
          aria-modal="true"
          aria-label={confirming === "stop" ? "Stop this turn?" : "End the session?"}
          onClick={(e) => e.stopPropagation()}
        >
          <p className="member-modal-note">
            {STOP_PROMISE}
            {confirming === "end" ? ` ${END_STOPS_FIRST}` : ""}
          </p>
          <div className="member-modal-actions">
            <button className="btn" onClick={() => setConfirming(null)}>
              Cancel
            </button>
            <button
              className="btn btn-danger"
              autoFocus
              onClick={confirming === "stop" ? stopNow : endNow}
            >
              {confirming === "stop" ? "Stop this turn" : "Stop and end"}
            </button>
          </div>
        </div>
      </div>
    )}
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
          {turnRunning && !ended && (
            <button
              className="btn btn-danger"
              disabled={stopping}
              onClick={() => setConfirming("stop")}
            >
              {stopping ? "Stopping…" : "Stop"}
            </button>
          )}
          <button
            className="btn"
            disabled={ended || ending}
            onClick={() => (turnRunning ? setConfirming("end") : endNow())}
          >
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
              <>
                {/* The same typing line the main chat has, on the same
                    presence store: the builder types in here like anyone
                    else, and the modal was the one room that never said so
                    (docket item 4). */}
                <TypingLine channelId={session.channel_id} />
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
                  onSent={(m) => {
                    setNotice(null);
                    setPendingSend({
                      afterSeq: m.seq,
                      afterStages: session.stages.length,
                      at: Date.now(),
                    });
                  }}
                  focusNonce={focusNonce}
                />
              </>
            )}
          </div>

          <div className="app-build-preview">
            <ol className="stage-bar">
              {APP_STAGES.map((stage, i) => (
                <li
                  key={stage}
                  className={`stage-step${
                    i < reached ? " done" : i === reached ? " current" : ""
                  }${i === reached && halted !== null ? " halted" : ""}`}
                >
                  <span className="stage-dot" aria-hidden />
                  <span className="stage-label">
                    {(i === reached ? currentLabel : null) ??
                      APP_STAGE_LABELS[stage]}
                  </span>
                </li>
              ))}
            </ol>
            {halted !== null && (
              <div className="app-build-chip app-build-chip--halted" role="status">
                Turn {turn?.turn ?? 0} {HALT_CHIP_TEXT[halted]}
              </div>
            )}
            <div className="app-elapsed">
              <span className="app-elapsed-label">Elapsed</span>
              <span className="app-elapsed-clock">
                {elapsedSince(session.started_at, clockAt)}
              </span>
            </div>
            {turn !== null && turn.files.length > 0 && (
              <div className="app-file-scroll">
                <div className="app-file-scroll-head">
                  Turn {turn.turn} — {countFiles(turn.files)} files
                </div>
                <ul className="app-file-list">
                  {turn.files.map((file, i) => (
                    /* Plain text, every entry — these are paths a build seat
                       chose, and the `+N more` marker is one of them. Keyed by
                       position because a turn may legitimately touch the same
                       path twice in one capped list. */
                    <li key={`${i}:${file}`}>{file}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className={`app-preview-frozen${frozen ? " frozen" : ""}`}>
              {(frozen || previewUrl === null) && (
                <div className="app-preview-banner">
                  {frozen ? (
                    <>
                      <span>Build in progress — the preview is frozen.</span>
                      {pendingStale && (
                        <button
                          className="btn app-unfreeze"
                          title="Nothing has come back from the builder; this puts the preview back in your hands."
                          onClick={() => setPendingSend(null)}
                        >
                          the builder hasn’t answered — unfreeze
                        </button>
                      )}
                    </>
                  ) : (
                    (previewNote ?? "No preview yet.")
                  )}
                </div>
              )}
              {previewUrl === null ? (
                <div className="app-preview-empty" aria-hidden />
              ) : (
                <div className="app-preview-frame">
                  {/* sandbox is EXACTLY the two flags the walls allow (wall 4).
                      The key remounts the frame on each deploy so a re-mint
                      that happened to hand back the same URL still reloads. */}
                  <iframe
                    key={deployedCount}
                    className="app-preview-iframe"
                    src={previewUrl}
                    sandbox="allow-scripts allow-same-origin"
                    title={`${app.name} — preview`}
                  />
                  {frozen && <div className="app-preview-overlay" aria-hidden />}
                </div>
              )}
            </div>
            {notice !== null && (
              <div className="app-build-chip app-build-chip--ok" role="status">
                {notice}
              </div>
            )}
            {blockedUrl !== null && (
              <p className="app-open-blocked">
                {POPUP_BLOCKED_NOTE}{" "}
                <a href={blockedUrl} target="_blank" rel="noopener noreferrer">
                  {app.name}
                </a>
              </p>
            )}
            <div className="app-preview-actions">
              <button
                className="btn btn-primary"
                disabled={
                  frozen || ended || busy !== null || deployedCount === 0
                }
                title={
                  ended
                    ? "This session ended."
                    : frozen
                      ? "Enabled when the builder idles."
                      : deployedCount > 0
                        ? "Publish what you see to the live app"
                        : "Enabled once a turn has deployed."
                }
                onClick={goLiveNow}
              >
                {busy === "live" ? "Publishing…" : "Live"}
              </button>
              <button
                className="btn"
                disabled={
                  busy !== null || app.status !== "live" || originBase === ""
                }
                title={
                  originBase === ""
                    ? NO_GATE
                    : app.status === "live"
                      ? "Open the live app in a new tab"
                      : "Enabled when the build reaches live."
                }
                onClick={openLive}
              >
                {busy === "open" ? "Opening…" : "Open"}
              </button>
              <button
                className="btn"
                disabled={busy !== null || app.status !== "live"}
                title={
                  app.status === "live"
                    ? "Post this app into a channel"
                    : "Enabled when the build reaches live."
                }
                onClick={() => {
                  setShareError(null);
                  setShareOpen(true);
                }}
              >
                Share
              </button>
              <button
                className="btn"
                disabled={frozen || ended || busy !== null}
                title={
                  ended
                    ? "This session ended."
                    : frozen
                      ? "Enabled when the builder idles."
                      : "Put the caret in the message box"
                }
                onClick={() => {
                  setShowPreview(false);
                  setFocusNonce((n) => n + 1);
                }}
              >
                Change something
              </button>
              {/* Revert only exists once there is a previous live to go back
                  to — the card is the only place that knows (D7). */}
              {card?.data?.has_previous_live === true &&
                (revertArmed ? (
                  <>
                    <button
                      className="btn btn-danger"
                      disabled={busy !== null}
                      onClick={revertNow}
                    >
                      {busy === "revert"
                        ? "Reverting…"
                        : "Revert to the previous live"}
                    </button>
                    <button className="btn" onClick={() => setRevertArmed(false)}>
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    className="btn"
                    disabled={busy !== null}
                    title="Put the previous live version back"
                    onClick={() => setRevertArmed(true)}
                  >
                    Revert
                  </button>
                ))}
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
      {shareOpen && (
        <ShareDialog
          appName={app.name}
          visibility={app.visibility}
          busy={busy === "share"}
          error={shareError}
          onShare={shareNow}
          onClose={() => setShareOpen(false)}
        />
      )}
    </>
  );
}
