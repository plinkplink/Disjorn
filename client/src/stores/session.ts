import { create } from "zustand";

import {
  ApiError,
  changePassword as apiChangePassword,
  fetchMe,
  login as apiLogin,
  logout as apiLogout,
  setRotationHandler,
  updateMe,
} from "../api";
import type { SettableStatus, User } from "../types";

interface SessionState {
  user: User | null;
  /** True while the initial GET /me probe is in flight (app boot). */
  booting: boolean;
  /** Last login error (server `detail`), cleared on retry/success. */
  loginError: string | null;
  loggingIn: boolean;

  /**
   * The account must rotate its password before it can do anything else.
   *
   * Learned from a 403, never from GET /me — the public User shape does not
   * carry the flag. Any walled-off request sets this (see setRotationHandler
   * in api.ts), so the app cannot end up in the state that shipped on
   * 2026-08-14: every route 403ing with no screen that could clear it.
   */
  mustChangePassword: boolean;
  changePasswordError: string | null;
  changingPassword: boolean;
  changePassword: (currentPassword: string, newPassword: string) => Promise<boolean>;

  /** App boot: resolve the session cookie into a user (or null). */
  bootstrap: () => Promise<void>;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  /** Persist a status change (also mirror it over WS via ws.sendStatus). */
  setStatus: (status: SettableStatus) => Promise<void>;
  setUser: (user: User | null) => void;
}

export const useSession = create<SessionState>()((set, get) => ({
  user: null,
  booting: true,
  loginError: null,
  loggingIn: false,
  mustChangePassword: false,
  changePasswordError: null,
  changingPassword: false,

  bootstrap: async () => {
    try {
      const user = await fetchMe();
      set({ user, booting: false });
    } catch {
      set({ user: null, booting: false });
    }
  },

  changePassword: async (currentPassword, newPassword) => {
    set({ changingPassword: true, changePasswordError: null });
    try {
      await apiChangePassword(currentPassword, newPassword);
    } catch (err) {
      set({
        changingPassword: false,
        changePasswordError:
          err instanceof ApiError ? err.detail : "Could not change password",
      });
      return false;
    }
    // Clear the wall BEFORE re-reading the user: fetchMe goes through the same
    // request path, and a stale flag would just re-arm on the next 403.
    set({ mustChangePassword: false, changingPassword: false });
    try {
      set({ user: await fetchMe() });
    } catch {
      /* the password did change; a failed refresh is not worth undoing it */
    }
    return true;
  },

  login: async (username, password) => {
    set({ loggingIn: true, loginError: null });
    try {
      const user = await apiLogin(username, password);
      set({ user, loggingIn: false, mustChangePassword: false });
      return true;
    } catch (err) {
      set({
        loggingIn: false,
        loginError: err instanceof ApiError ? err.detail : "Login failed",
      });
      return false;
    }
  },

  logout: async () => {
    try {
      await apiLogout();
    } catch {
      /* clearing local state matters more than the server ack */
    }
    set({ user: null, mustChangePassword: false, changePasswordError: null });
  },

  setStatus: async (status) => {
    const user = get().user;
    if (user === null) return;
    set({ user: { ...user, status } }); // optimistic
    try {
      const updated = await updateMe({ status });
      set({ user: updated });
    } catch {
      set({ user }); // roll back
    }
  },

  setUser: (user) => set({ user }),
}));

// Wire the api layer's rotation signal into this store. Done at module load so
// it is armed before the first request of the session — including bootstrap's
// GET /me, which is exempt, and whatever the shell asks for immediately after,
// which is not.
setRotationHandler(() => {
  if (!useSession.getState().mustChangePassword) {
    useSession.setState({ mustChangePassword: true });
  }
});
