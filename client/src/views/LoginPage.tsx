import { useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../stores/session";

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
      <form className="login-card" onSubmit={onSubmit}>
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
          <p className="field-hint" id="login-help">
            Disjorn does not send email, so there is no reset link. Ask an
            admin to reset your password. They will hand you a temporary one,
            and the next time you log in you will be asked to choose your own.
          </p>
        )}
      </form>
    </div>
  );
}
