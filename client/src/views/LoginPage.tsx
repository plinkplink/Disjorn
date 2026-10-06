import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError, requestPasswordReset } from "../api";
import { useSession } from "../stores/session";

/** Mirrors NOTE_MAX_LENGTH in server/app/routers/reset_requests.py. */
const RESET_NOTE_MAX = 200;

function ResetRequestForm({ initialUsername }: { initialUsername: string }) {
  const [username, setUsername] = useState(initialUsername);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const name = username.trim();
    if (name.length === 0 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await requestPasswordReset(name, note.trim());
      setSent(res.detail);
      setNote("");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not send the request");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="login-form" onSubmit={(e) => void onSubmit(e)}>
      <h2 className="login-help-title">Ask an admin to reset it</h2>
      <div className="field">
        <label htmlFor="reset-request-username">Username</label>
        <input
          id="reset-request-username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          maxLength={64}
        />
      </div>
      <div className="field">
        <label htmlFor="reset-request-note">Note for the admin (optional)</label>
        <textarea
          id="reset-request-note"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={RESET_NOTE_MAX}
          rows={2}
          aria-describedby="reset-request-note-hint"
        />
        <p className="field-hint" id="reset-request-note-hint">
          {note.length}/{RESET_NOTE_MAX}. Never put a password here.
        </p>
      </div>
      {error !== null && <p className="form-error">{error}</p>}
      {sent !== null && (
        <p className="field-hint" role="status">
          {sent}
        </p>
      )}
      <button className="btn" type="submit" disabled={busy || username.trim().length === 0}>
        {busy ? "Asking…" : "Ask an admin"}
      </button>
    </form>
  );
}

export function LoginPage() {
  const login = useSession((s) => s.login);
  const loginError = useSession((s) => s.loginError);
  const loggingIn = useSession((s) => s.loggingIn);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showHelp, setShowHelp] = useState(false);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (username.length === 0 || password.length === 0 || loggingIn) return;
    void login(username, password);
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <form className="login-form" onSubmit={onSubmit}>
          <h1>Disjorn</h1>
          <p className="tagline">Welcome back.</p>
          <div className="field">
            <label htmlFor="login-username">Username</label>
            <input
              id="login-username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
            />
          </div>
          <div className="field">
            <label htmlFor="login-password">Password</label>
            <input
              id="login-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          {loginError !== null && <p className="form-error">{loginError}</p>}
          <button className="btn btn-primary" type="submit" disabled={loggingIn}>
            {loggingIn ? "Logging in…" : "Log in"}
          </button>
        </form>
        <button
          type="button"
          className="link-btn login-help-toggle"
          aria-expanded={showHelp}
          aria-controls="login-help"
          onClick={() => setShowHelp((v) => !v)}
        >
          Forgot your password?
        </button>
        {showHelp && (
          <div className="login-form" id="login-help">
            <p className="field-hint">
              Disjorn does not send email, so there is no reset link. Ask an
              admin to reset your password. They will hand you a temporary one,
              and the next time you log in you will be asked to choose your own.
            </p>
            <ResetRequestForm initialUsername={username.trim()} />
          </div>
        )}
      </div>
    </div>
  );
}
