/* Which verb an app offers its viewer. The server rules on every one of them
 * (ownership, quota, the lock); this only decides which to try. */

import type { App } from "../types";

export type AppRowAction =
  | { kind: "resume"; sessionId: number }
  | { kind: "start" }
  | { kind: "card" };

function isMine(app: App, myId: number | null): boolean {
  return myId !== null && app.owner_user_id === myId;
}

/** A click on an app row. `open_session` is listed to every viewer of the app,
    but a session is readable by its owner alone, so only the owner resumes. */
export function appRowAction(app: App, myId: number | null): AppRowAction {
  if (!isMine(app, myId)) return { kind: "card" };
  if (app.open_session !== null) {
    return { kind: "resume", sessionId: app.open_session.id };
  }
  return app.status === "draft" ? { kind: "start" } : { kind: "card" };
}

/** Keep building: a session on the owner's own live app, where Remix would
    make a second app. */
export function canKeepBuilding(app: App, myId: number | null): boolean {
  return isMine(app, myId) && app.status === "live";
}
