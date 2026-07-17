import { useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../stores/session";

export function LoginPage() {
  const login = useSession((s) => s.login);
  const loginError = useSession((s) => s.loginError);
  const loggingIn = useSession((s) => s.loggingIn);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

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
      </form>
    </div>
  );
}
