import { create } from "zustand";

import { ApiError, fetchMe, login as apiLogin, logout as apiLogout, updateMe } from "../api";
import type { SettableStatus, User } from "../types";

interface SessionState {
  user: User | null;
  /** True while the initial GET /me probe is in flight (app boot). */
  booting: boolean;
  /** Last login error (server `detail`), cleared on retry/success. */
  loginError: string | null;
  loggingIn: boolean;

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

  bootstrap: async () => {
    try {
      const user = await fetchMe();
      set({ user, booting: false });
    } catch {
      set({ user: null, booting: false });
    }
  },

  login: async (username, password) => {
    set({ loggingIn: true, loginError: null });
    try {
      const user = await apiLogin(username, password);
      set({ user, loggingIn: false });
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
    set({ user: null });
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
