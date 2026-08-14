import { useState } from "react";
import type { FormEvent } from "react";

import { PASSWORD_MIN_LENGTH } from "../api";
import { useSession } from "../stores/session";

/**
 * The screen that makes the rotation gate survivable.
 *
 * The server half of password rotation shipped on 2026-08-14 without this, and
 * the result was that every human account was walled off from every route with
 * no way to comply from inside the app. The server was right; there was just
 * nowhere to type a new password. This is that place.
 *
 * It renders INSTEAD of the app shell — not as a modal over it — because while
 * the flag is set there is no app to be over: every other request 403s.
 */
export function ChangePasswordPage() {
  const user = useSession((s) => s.user);
  const changePassword = useSession((s) => s.changePassword);
  const serverError = useSession((s) => s.changePasswordError);
  const busy = useSession((s) => s.changingPassword);

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");

  // Checked here only to save a round trip and to say so before the user
  // submits; the server enforces both rules regardless.
  const tooShort = next.length > 0 && next.length < PASSWORD_MIN_LENGTH;
  const mismatch = confirm.length > 0 && next !== confirm;
  const ready =
    current.length > 0 &&
    next.length >= PASSWORD_MIN_LENGTH &&
    next === confirm &&
    !busy;

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    void changePassword(current, next);
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>Choose a new password</h1>
        <p className="tagline">
          {user === null
            ? "Your password needs replacing before you can carry on."
            : `Hi ${user.display_name} — your current password was set by an admin, so it needs replacing before you can carry on.`}
        </p>

        <div className="field">
          <label htmlFor="pw-current">Current password</label>
          <input
            id="pw-current"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            autoFocus
          />
        </div>

        <div className="field">
          <label htmlFor="pw-new">New password</label>
          <input
            id="pw-new"
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            aria-describedby="pw-new-hint"
          />
          <p className="field-hint" id="pw-new-hint">
            At least {PASSWORD_MIN_LENGTH} characters. No other rules — length
            is what helps.
          </p>
        </div>

        <div className="field">
          <label htmlFor="pw-confirm">New password again</label>
          <input
            id="pw-confirm"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
        </div>

        {tooShort && (
          <p className="form-error">
            That is {next.length} character{next.length === 1 ? "" : "s"} — it
            needs {PASSWORD_MIN_LENGTH}.
          </p>
        )}
        {mismatch && <p className="form-error">The two new passwords do not match.</p>}
        {serverError !== null && <p className="form-error">{serverError}</p>}

        <button className="btn btn-primary" type="submit" disabled={!ready}>
          {busy ? "Saving…" : "Change password"}
        </button>

        <p className="field-hint">
          Changing it signs out your other devices. This one stays signed in.
        </p>
      </form>
    </div>
  );
}
