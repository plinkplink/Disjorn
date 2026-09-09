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
  fetchAppSession,
  heartbeatAppSession,
  listApps,
  listBuilders,
  patchApp,
  removeAppFromMenu,
  startAppSession,
} from "../api";
import type {
  App,
  AppSession,
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

  return {
    apps: [],
    quota: null,
    builders: [],
    sessions: {},
    loaded: false,

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
    },

    onAppUpdate: (frame) => {
      const { app } = frame;
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
