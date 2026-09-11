/* The apps registry, client side (SPECS/2026-08-30-apps-tab-v1.md, stage 1;
 * SPECS/2026-09-06-apps-builder-seat.md §J, stage 2).
 *
 * This store is the CONSUMER end of the stage-event stream. A frame arriving
 * here updates the session's `stages` and `stage`, and every renderer reads
 * those. The stage bar in AppBuildModal is one subscriber. A second one (a
 * build log, a sidebar dot) can be added by subscribing to this store and
 * touching nothing else — no publisher change, no new plumbing.
 *
 * Stage 2 gave the stream a TURN, and the one derived thing this store keeps
 * is `session.lastTurn`: what the newest turn wrote, whether it halted, and
 * whether it is over. It is DERIVED, from `stages`, by one function used both
 * by the live frame handler and by the reload path — a store that computed it
 * two ways would show a reloaded modal a different turn than a live one.
 *
 * Like every store here it talks to `api`, never to `ws`: the socket imports
 * stores, stores never import the socket, and that is what keeps the import
 * graph acyclic.
 */

import { create } from "zustand";

import {
  addAppToMenu,
  endAppSession,
  stopAppTurn,
  fetchAppCard,
  fetchAppSession,
  fetchAppsConfig,
  goAppLive,
  heartbeatAppSession,
  listApps,
  listBuilders,
  openAppUrl,
  patchApp,
  remixApp,
  removeAppFromMenu,
  revertAppLive,
  shareApp,
  startAppSession,
} from "../api";
import type {
  App,
  AppCardData,
  AppRoot,
  AppSession,
  AppStage,
  AppStageFrame,
  AppUpdateFrame,
  Builder,
  HaltReason,
  Quota,
  StageEvent,
  TurnState,
} from "../types";

/* An `app_stage` frame carries no row id — the persisted event has one, the
   frame does not (brief D7). Live-appended events get a descending negative
   id so React keys stay stable and a later GET, whose events carry real
   positive ids, can never collide with one. */
let syntheticStageId = -1;

function stageEventFromFrame(frame: AppStageFrame): StageEvent {
  return {
    id: syntheticStageId--,
    session_id: frame.session_id,
    stage: frame.stage,
    detail: frame.detail,
    created_at: frame.created_at,
  };
}

const HALT_REASONS: readonly HaltReason[] = [
  "timeout",
  "error",
  "secret",
  "ceiling",
  "stopped",
];

function haltOf(value: unknown): HaltReason | null {
  return HALT_REASONS.includes(value as HaltReason)
    ? (value as HaltReason)
    : null;
}

/**
 * The newest turn a session's events describe, or null before the first one.
 *
 * Folded over the WHOLE list rather than read off the last event, because one
 * turn arrives as several events and only some of them carry each fact: the
 * files come with `files_written`, the halt may come on a re-posted `scoped`,
 * and `deployed` carries nothing but the number. A newer turn number wipes
 * the slate — that is how a halted chip clears when the next turn starts
 * (§B4), and it is the only thing that clears it.
 */
function turnFromStages(stages: StageEvent[]): TurnState | null {
  let state: TurnState | null = null;
  for (const event of stages) {
    const detail = event.detail ?? {};
    const turn = typeof detail.turn === "number" ? detail.turn : null;
    if (turn === null) continue;
    if (state === null || turn > state.turn) {
      state = {
        turn,
        files: [],
        halted: null,
        no_changes: false,
        summary: null,
        done: false,
      };
    } else if (turn < state.turn) {
      continue; // an out-of-order event about a turn that is already history
    }
    if (Array.isArray(detail.files)) {
      state.files = detail.files.filter((f): f is string => typeof f === "string");
    }
    if (typeof detail.summary === "string") state.summary = detail.summary;
    const halted = haltOf(detail.halted);
    if (halted !== null) {
      state.halted = halted;
      state.done = true;
    }
    if (event.stage === "files_written") {
      state.no_changes = detail.no_changes === true;
      if (state.no_changes) state.done = true;
    }
  }
  return state;
}

function withTurn(session: AppSession): AppSession {
  return { ...session, lastTurn: turnFromStages(session.stages) };
}

/**
 * Stages after which nothing is changing under the user's mouse.
 *
 * `files_written` is in here for the same reason it is the bar's terminal
 * stage: the turn's diff is on disk. `deployed` and `live` are after it. A
 * turn that halts is caught by `lastTurn.done` instead, whatever stage it
 * reached.
 */
// `files_written` is NOT settled: a turn that wrote files is still being
// published, and `deployed` follows it with a reload. Thawing between the two
// would change the app under the mouse (Round 5). A turn that wrote nothing
// ends at `files_written` with `no_changes`, and a halt ends wherever it was —
// both of those set `lastTurn.done`, which is checked first.
const TURN_SETTLED_STAGES: readonly AppStage[] = ["deployed", "live"];

/**
 * Is a turn running right now? — the one input to the preview's freeze/thaw
 * (stage 3 D10).
 *
 * DERIVED from exactly what the stage bar reads: the session's current stage
 * and the turn the store already folded out of the events. There is no
 * running flag anywhere, on the wire or in this store, because a second
 * source of truth for "is it building" is how a frozen frame and an idle bar
 * end up on screen together.
 *
 * A turn starts at `scoped` and runs until it settles (files on disk, or
 * further) or halts. Before the first turn — the talking phase — nothing is
 * running: the frame is the user's to click.
 */
export function isTurnRunning(session: AppSession | undefined): boolean {
  if (session === undefined) return false;
  const turn = session.lastTurn ?? null;
  if (turn === null || turn.done) return false;
  const stage = session.stage;
  if (stage === null) return false;
  return !TURN_SETTLED_STAGES.includes(stage);
}

/**
 * How many times this session has reached `deployed`.
 *
 * Two things read it and they must not drift: Live has nothing to publish
 * until the count is at least one (the button says so rather than collecting
 * a 409), and the preview reloads on each increment, a new deploy being new
 * files behind the same URL.
 */
export function deployedTurnCount(session: AppSession | undefined): number {
  return (session?.stages ?? []).filter((e) => e.stage === "deployed").length;
}

/**
 * One app's card, as the message list sees it.
 *
 * `missing` is the 404 — not entitled, or gone — and it is CACHED like any
 * other answer so a channel full of the same unreachable link does not
 * re-ask once per row. The one thing that clears it is a status frame saying
 * the app moved (`retryMissingCard`); nothing else, and no timer.
 */
export interface CardEntry {
  status: "loading" | "done" | "missing";
  data: AppCardData | null;
}

interface AppsState {
  /** The apps on my menu, as GET /apps returns them. */
  apps: App[];
  /** My build meter. Null until the first load answers. */
  quota: Quota | null;
  /** The resident seats offered as builders (GET /apps/builders). */
  builders: Builder[];
  /** Sessions we have loaded or started, by session id. */
  sessions: Record<number, AppSession>;
  loaded: boolean;
  /** The serving gate's origin, or "" when this house has no gate (D1).
      Every URL-building path reads this first; nobody guesses one. */
  originBase: string;
  /** GET /apps/{id}/card answers, by app id. */
  cards: Record<string, CardEntry>;
  /**
   * A session the shell should open the build modal on.
   *
   * A seam, not a router: Remix happens down inside a message row and inside
   * the chooser, and neither of those can open a modal that AppShell owns.
   * The alternative was threading a callback through MessageList — which is
   * also rendered INSIDE the build modal, where "open another build modal" is
   * not a thing that can happen. AppShell consumes this and clears it.
   */
  pendingSessionId: number | null;

  /** GET /apps — boot, after any menu change, and on WS reconnect resync. */
  refresh: () => Promise<void>;
  /** GET /apps/builders. Cheap and idempotent; the sidebar needs it for the
      builder avatar on every app row, not just the chooser. */
  loadBuilders: () => Promise<void>;
  /** POST /apps/sessions. Throws ApiError (429 quota, 409 lock, 400 builder)
      with the server's own sentence — show it, do not translate it. */
  startSession: (builderBotId: number, appId?: string) => Promise<AppSession>;
  loadSession: (sessionId: number) => Promise<AppSession>;
  /** Extends the lock. Rethrows so the caller can see a 410 (ended/lapsed). */
  heartbeat: (sessionId: number) => Promise<void>;
  endSession: (sessionId: number) => Promise<void>;
  /** POST /apps/sessions/{id}/stop. Rethrows so the modal can read a 409 (no
      turn running) or 410 (ended) as the outcome it is. */
  stopTurn: (sessionId: number) => Promise<void>;
  renameApp: (appId: string, name: string) => Promise<void>;
  addToMenu: (appId: string) => Promise<void>;
  removeFromMenu: (appId: string) => Promise<void>;

  /* ---- the serving gate (stage 3) ---- */

  /** GET /apps/config. Failure leaves `originBase` "" — the same state as a
      house with no gate, which is the honest reading of "we could not ask". */
  loadConfig: () => Promise<void>;
  /** POST /apps/{id}/open → the URL to frame or open. Rethrows so a caller
      can tell 503 (no gate) from 409 (not live) and say which. */
  openApp: (appId: string, root?: AppRoot) => Promise<string>;
  /** POST /apps/sessions/{id}/live. Rethrows the 409. */
  goLive: (sessionId: number) => Promise<AppSession>;
  /** POST /apps/{id}/revert. Rethrows the 409 (nothing to go back to). */
  revertApp: (appId: string) => Promise<void>;
  /** POST /apps/{id}/share → the id of the message that landed. */
  shareApp: (
    appId: string,
    channelId: number,
    visibility?: "shared" | "public",
  ) => Promise<number>;
  /** POST /apps/{id}/remix → a session on the CHILD app. */
  remixApp: (appId: string, builderBotId?: number) => Promise<AppSession>;
  /** GET /apps/{id}/card, once per app id. Never throws: a 404 is cached as
      `missing`, and the card renders nothing rather than an error. `force`
      re-asks after something the card reports has moved. */
  loadCard: (appId: string, force?: boolean) => Promise<void>;
  /** Hand a session to AppShell to open the build modal on. */
  requestBuildModal: (sessionId: number) => void;
  clearBuildModalRequest: () => void;

  /* ---- WS frame handlers (owner's sockets only) ---- */
  onStage: (frame: AppStageFrame) => void;
  onAppUpdate: (frame: AppUpdateFrame) => void;
}

export const useApps = create<AppsState>()((set, get) => {
  /** Rewrite one app row wherever it is held: the menu list, and the `app`
      carried by any loaded session. One place, so no view can see two
      versions of the same app. */
  const patchAppEverywhere = (appId: string, patch: (app: App) => App): void => {
    const sessions: Record<number, AppSession> = {};
    for (const [key, session] of Object.entries(get().sessions)) {
      sessions[Number(key)] =
        session.app.id === appId
          ? { ...session, app: patch(session.app) }
          : session;
    }
    set({
      apps: get().apps.map((a) => (a.id === appId ? patch(a) : a)),
      sessions,
    });
  };

  /**
   * A card that failed once gets exactly one more chance, when a frame says
   * the app moved.
   *
   * `missing` is cached deliberately — a channel full of the same unreachable
   * link must not ask the gate once per row. But the cache had no way out
   * (Claudette #2555): a card asked for during a network blip, or asked for
   * while the app was still a draft, stayed blank for the rest of the session
   * even after the app went live. A status frame is the one honest signal that
   * the answer may have changed, so that — and ONLY that — clears the entry.
   * Never a timer, never a re-render: a 404 that means "not entitled" is a
   * permanent answer for a stranger, and a stranger gets no frames at all
   * (both `app_stage` and `app_update` go to the owner's sockets only), so
   * this cannot become a retry loop against the wall.
   */
  const retryMissingCard = (appId: string): void => {
    if (get().cards[appId]?.status !== "missing") return;
    void get().loadCard(appId, true);
  };

  /** Keep a cached card's status honest when the app row moves under it. A
      card that says `draft` next to an Open button that works is worse than
      no chip; the card endpoint is the source, this only follows it. */
  const patchCardStatus = (appId: string, status: App["status"]): void => {
    const entry = get().cards[appId];
    if (entry === undefined || entry.data === null) return;
    if (entry.data.status === status) return;
    set({
      cards: {
        ...get().cards,
        [appId]: { ...entry, data: { ...entry.data, status } },
      },
    });
  };

  return {
    apps: [],
    quota: null,
    builders: [],
    sessions: {},
    loaded: false,
    originBase: "",
    cards: {},
    pendingSessionId: null,

    refresh: async () => {
      const { apps, quota } = await listApps();
      set({ apps, quota, loaded: true });
    },

    loadBuilders: async () => {
      set({ builders: await listBuilders() });
    },

    startSession: async (builderBotId, appId) => {
      const session = withTurn(await startAppSession(builderBotId, appId));
      set({
        sessions: { ...get().sessions, [session.id]: session },
        quota: session.quota,
      });
      // The new app (or the newly locked one) belongs in the sidebar now.
      await get().refresh();
      return session;
    },

    loadSession: async (sessionId) => {
      // The reload path and the live path both go through withTurn, so a
      // modal reopened mid-build shows the same turn a modal that never
      // closed is showing.
      const session = withTurn(await fetchAppSession(sessionId));
      set({
        sessions: { ...get().sessions, [sessionId]: session },
        quota: session.quota,
      });
      return session;
    },

    heartbeat: async (sessionId) => {
      await heartbeatAppSession(sessionId);
    },

    stopTurn: async (sessionId) => {
      await stopAppTurn(sessionId);
    },

    endSession: async (sessionId) => {
      await endAppSession(sessionId);
      const session = get().sessions[sessionId];
      if (session !== undefined) {
        set({
          sessions: {
            ...get().sessions,
            [sessionId]: {
              ...session,
              ended_at: session.ended_at ?? new Date().toISOString(),
            },
          },
        });
      }
      // The lock is gone, so the row's `open_session` is too; the server's
      // answer is the authority on that, not this optimistic edit.
      await get().refresh();
    },

    renameApp: async (appId, name) => {
      const updated = await patchApp(appId, { name });
      patchAppEverywhere(appId, () => updated);
    },

    addToMenu: async (appId) => {
      await addAppToMenu(appId);
      await get().refresh();
    },

    removeFromMenu: async (appId) => {
      await removeAppFromMenu(appId);
      await get().refresh();
    },

    loadConfig: async () => {
      try {
        const config = await fetchAppsConfig();
        set({ originBase: config.origin_base });
      } catch {
        // A house whose config cannot be read is, to this client, a house
        // with no gate: nothing gets framed and every button says so.
        set({ originBase: "" });
      }
    },

    openApp: async (appId, root) => {
      const { url } = await openAppUrl(appId, root);
      return url;
    },

    goLive: async (sessionId) => {
      const session = withTurn(await goAppLive(sessionId));
      set({
        sessions: { ...get().sessions, [session.id]: session },
        quota: session.quota,
      });
      // The row's chip and any card on screen follow the app the server just
      // published, without waiting for the next GET /apps.
      patchAppEverywhere(session.app.id, () => session.app);
      patchCardStatus(session.app.id, session.app.status);
      // Publishing rotated `live` → `live.prev`, so `has_previous_live` moved
      // and only the card endpoint knows the new answer.
      if (get().cards[session.app.id] !== undefined) {
        void get().loadCard(session.app.id, true);
      }
      return session;
    },

    revertApp: async (appId) => {
      const { status } = await revertAppLive(appId);
      patchAppEverywhere(appId, (app) => ({ ...app, status }));
      patchCardStatus(appId, status);
      // The swap consumed the previous live; re-ask rather than guess.
      if (get().cards[appId] !== undefined) {
        void get().loadCard(appId, true);
      }
    },

    shareApp: async (appId, channelId, visibility) => {
      const { message_id } = await shareApp(appId, channelId, visibility);
      // Sharing changes the app's visibility server-side; the row carries it.
      await get().refresh();
      // The copy of that row a build session holds must follow, or the share
      // dialog — which seeds its Public box from the app's visibility — would
      // reopen showing the state it just changed (Claudette #2555).
      const fresh = get().apps.find((a) => a.id === appId);
      if (fresh !== undefined) patchAppEverywhere(appId, () => fresh);
      return message_id;
    },

    remixApp: async (appId, builderBotId) => {
      const session = withTurn(await remixApp(appId, builderBotId));
      set({
        sessions: { ...get().sessions, [session.id]: session },
        quota: session.quota,
      });
      // The child app is mine and on my menu now.
      await get().refresh();
      return session;
    },

    loadCard: async (appId, force = false) => {
      if (!force && get().cards[appId] !== undefined) return;
      set({
        cards: { ...get().cards, [appId]: { status: "loading", data: null } },
      });
      try {
        const data = await fetchAppCard(appId);
        set({ cards: { ...get().cards, [appId]: { status: "done", data } } });
      } catch {
        // 404 (not entitled) and every other failure land in the same place:
        // the link stands on its own and the card renders nothing.
        set({
          cards: { ...get().cards, [appId]: { status: "missing", data: null } },
        });
      }
    },

    requestBuildModal: (sessionId) => set({ pendingSessionId: sessionId }),

    clearBuildModalRequest: () => set({ pendingSessionId: null }),

    onStage: (frame) => {
      const session = get().sessions[frame.session_id];
      if (session !== undefined) {
        set({
          sessions: {
            ...get().sessions,
            [frame.session_id]: withTurn({
              ...session,
              stage: frame.stage,
              stages: [...session.stages, stageEventFromFrame(frame)],
            }),
          },
        });
      }
      // `live` is also a status change the server writes on the app row
      // itself; mirror it so the sidebar chip does not wait for a refresh.
      patchAppEverywhere(frame.app_id, (app) => ({
        ...app,
        status: frame.stage === "live" ? "live" : app.status,
        open_session:
          app.open_session !== null && app.open_session.id === frame.session_id
            ? { ...app.open_session, stage: frame.stage }
            : app.open_session,
      }));
      if (frame.stage === "live") patchCardStatus(frame.app_id, "live");
      // A card that 404'd before this session got anywhere may answer now.
      retryMissingCard(frame.app_id);
    },

    onAppUpdate: (frame) => {
      const { app } = frame;
      // The frame carries the whole row, so a status change rides in on it;
      // any card already on screen for this app follows it (stage 3).
      patchCardStatus(app.id, app.status);
      retryMissingCard(app.id);
      if (get().apps.some((a) => a.id === app.id)) {
        patchAppEverywhere(app.id, () => app);
        return;
      }
      // An app that is on my menu but not in my list yet (a rename racing the
      // first load): take the row the server just handed me. One that is NOT
      // on my menu has no place in the sidebar, so it is only merged into any
      // session holding it.
      if (app.on_menu) {
        set({ apps: [...get().apps, app] });
        return;
      }
      patchAppEverywhere(app.id, () => app);
    },
  };
});
