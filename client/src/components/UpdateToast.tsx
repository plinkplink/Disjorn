/* "A new version is ready" notice, shown when a newer service worker has
   installed and is waiting behind the running one (see src/pwa.ts).

   Dismiss hides it for this page life only — the worker keeps waiting, and
   the next natural reload picks it up regardless. That is deliberate: the
   toast is a courtesy, not a gate. */

import { useEffect, useState } from "react";

import { applyUpdate, onUpdateWaiting } from "../pwa";

export function UpdateToast() {
  const [waiting, setWaiting] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => onUpdateWaiting(setWaiting), []);

  if (!waiting || dismissed) return null;
  return (
    <div className="update-toast" role="status" aria-live="polite">
      <span className="update-toast-text">A new version of Disjorn is ready.</span>
      <button className="btn btn-primary" onClick={applyUpdate}>
        Reload
      </button>
      <button
        className="icon-btn"
        aria-label="Dismiss update notice"
        onClick={() => setDismissed(true)}
      >
        ✕
      </button>
    </div>
  );
}
