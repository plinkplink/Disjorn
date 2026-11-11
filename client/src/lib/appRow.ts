/* Which verb an app offers its viewer. The server rules on every one of them
 * (ownership, quota, the lock); this only decides which to try. */

import type { App } from "../types";

export type AppRowAction =
  | { kind: "resume"; sessionId: number }
  | { kind: "start" }
  | { kind: "finish" }
  | { kind: "card" };

function isMine(app: App, myId: number | null): boolean {
  return myId !== null && app.owner_user_id === myId;
}

/** A click on an app row. `open_session` is listed to every viewer of the app,
    but a session is readable by its owner alone, so only the owner resumes. */
export function appRowAction(app: App, myId: number | null): AppRowAction {
  if (!isMine(app, myId)) return { kind: "card" };
  // A delete that did not finish: sending it again is the row's only verb.
  if (app.status === "deleting") return { kind: "finish" };
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

/** Delete is offered to the owner at every status, a row with a session open
    included: the server refuses while a build is running. A row already
    `deleting` has no card to offer it on. */
export function canDeleteApp(app: App, myId: number | null): boolean {
  return isMine(app, myId) && app.status !== "deleting";
}

/** What the confirm dialog says other people lose, or null for an app nobody
    else could open. Only a live app is reachable by anyone but its owner. */
export function deleteReachLine(app: App): string | null {
  if (app.status !== "live") return null;
  if (app.visibility === "public") {
    return "It is live and public: everyone in the house loses it.";
  }
  if (app.visibility === "shared") {
    return "It is live and shared: the people it was shared with lose access.";
  }
  return "It is live: its link stops working.";
}
