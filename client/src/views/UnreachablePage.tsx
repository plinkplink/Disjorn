import { useEffect, useState } from "react";

import { useSession } from "../stores/session";

/**
 * Shown when the boot probe could not reach the server at all — never when the
 * server answered "not signed in". The distinction matters on a phone: an
 * installed PWA resuming from background loses its first request, and painting
 * the login form there reads as "the app logged me out", which it did not.
 *
 * The session cookie is untouched, so recovery is just "ask again": once when
 * the reader taps, and automatically whenever the OS says the app is back in
 * the foreground or back online.
 */
export function UnreachablePage() {
  const bootstrap = useSession((s) => s.bootstrap);
  const booting = useSession((s) => s.booting);
  const [retriedAt, setRetriedAt] = useState(0);

  useEffect(() => {
    const retry = () => {
      if (document.visibilityState === "hidden") return;
      setRetriedAt(Date.now());
      void bootstrap();
    };
    window.addEventListener("online", retry);
    document.addEventListener("visibilitychange", retry);
    return () => {
      window.removeEventListener("online", retry);
      document.removeEventListener("visibilitychange", retry);
    };
  }, [bootstrap]);

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Disjorn</h1>
        <p className="tagline">
          Can&rsquo;t reach the server. You are still signed in — this is the
          connection, not your session.
        </p>
        <button
          className="btn btn-primary"
          type="button"
          disabled={booting}
          onClick={() => {
            setRetriedAt(Date.now());
            void bootstrap();
          }}
        >
          {booting ? "Reconnecting…" : "Try again"}
        </button>
        {retriedAt > 0 && !booting && (
          <p className="form-error">Still no answer from the server.</p>
        )}
      </div>
    </div>
  );
}
