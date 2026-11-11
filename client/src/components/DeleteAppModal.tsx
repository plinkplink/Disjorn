/* "Delete app…" — the confirm dialog (SPECS/2026-11-11-apps-delete.md).

   No type-to-confirm: the sentence names the app and what goes with it, and
   the button is the destructive one. The server is the wall; a refusal is
   shown verbatim.

   The dialog holds its own copy of the app. A delete that fails after the
   server has deleted the app turns its row into a `deleting` one, which has
   no card; the same request sent again, from here or from that row, is what
   finishes the removal. */

import { useEffect, useState } from "react";

import { ApiError } from "../api";
import { deleteReachLine } from "../lib/appRow";
import { useApps } from "../stores/apps";
import type { App } from "../types";

export function DeleteAppModal({
  app,
  onClose,
}: {
  app: App;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The server deleted the app but could not finish removing its files. */
  const [unfinished, setUnfinished] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await useApps.getState().deleteApp(app.id);
    } catch (err) {
      setBusy(false);
      setError(
        err instanceof ApiError ? err.detail : "Could not delete the app",
      );
      setUnfinished(
        useApps
          .getState()
          .apps.some((a) => a.id === app.id && a.status === "deleting"),
      );
      return;
    }
    onClose();
  };

  const reach = deleteReachLine(app);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="delete-channel-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Delete ${app.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head">
          <span className="member-modal-title">Delete {app.name}</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>

        <p className="member-modal-note">
          This deletes <strong>{app.name}</strong> for good: the app, its build
          chat and history, and its preview and live pages. Nothing brings it
          back.
        </p>
        {reach !== null && <p className="member-modal-note">{reach}</p>}
        {app.open_session !== null && (
          <p className="member-modal-note">
            Its open build session is closed. A build that is running has to
            be stopped first.
          </p>
        )}
        <p className="member-modal-note">
          Remixes made from it, yours or anyone else&rsquo;s, stay as apps of
          their own.
        </p>
        <p className="member-modal-note">
          Nothing in Disjorn brings it back. A copy stays in the house&rsquo;s
          offsite backup, for up to a year, and only the server&rsquo;s keeper
          can restore from it.
        </p>

        {error !== null && <p className="form-error">{error}</p>}
        {unfinished && (
          <p className="member-modal-note">
            The app is deleted: it is off everyone else&rsquo;s list and no
            new link to it can be made. Some of its files are still on the
            house&rsquo;s disk, so a link opened earlier may still work. Try
            again here, or use Finish deleting on its row in your Apps list.
          </p>
        )}

        <div className="member-modal-actions">
          <button className="btn" onClick={onClose} disabled={busy}>
            {unfinished ? "Close" : "Cancel"}
          </button>
          <button
            className="btn btn-danger"
            onClick={() => void submit()}
            disabled={busy}
          >
            {busy ? "Deleting…" : error !== null ? "Try again" : "Delete app"}
          </button>
        </div>
      </div>
    </div>
  );
}
