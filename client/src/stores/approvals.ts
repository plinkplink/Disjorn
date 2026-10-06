/* Every write and every approval_update frame carries the whole composed
   proposal, so this store replaces its copy and never merges; an HTTP reply
   and a frame race, so a copy older than the stored one is dropped. */

import { create } from "zustand";

import { ApiError, approvalAct, approvalGet, approvalList } from "../api";
import type {
  ApprovalAction,
  ApprovalProposal,
  ApprovalUpdateFrame,
} from "../types";

export const MAX_REMARKS_CHARS = 4000;

/** "unavailable" is the disarmed server's 503; its detail is shown verbatim. */
export type ApprovalsStatus =
  | "idle"
  | "loading"
  | "ready"
  | "unavailable"
  | "error";

interface ApprovalsState {
  status: ApprovalsStatus;
  detail: string | null;
  proposals: ApprovalProposal[];
  truncated: boolean;

  refresh: () => Promise<void>;
  fetchOne: (id: number) => Promise<void>;
  act: (
    id: number,
    action: ApprovalAction,
    remarks: string,
  ) => Promise<ApprovalProposal>;
  onUpdate: (frame: ApprovalUpdateFrame) => void;
}

/** Every act stamps its row's acted_at and any closure with the same instant,
    and both are fixed-width UTC strings, so they order as text. */
function freshness(p: ApprovalProposal): [string, string] {
  let acted = "";
  for (const s of p.states) {
    if (s.acted_at !== null && s.acted_at > acted) acted = s.acted_at;
  }
  return [acted, p.closed_at ?? ""];
}

export function isOlder(next: ApprovalProposal, stored: ApprovalProposal): boolean {
  const [a, c] = freshness(next);
  const [b, d] = freshness(stored);
  return a < b || (a === b && c < d);
}

/** Replace-or-insert by id, newest (highest id) first; an older copy is ignored. */
export function upsertProposal(
  list: ApprovalProposal[],
  next: ApprovalProposal,
): ApprovalProposal[] {
  const stored = list.find((p) => p.id === next.id);
  if (stored !== undefined && isOlder(next, stored)) return list;
  const rest = list.filter((p) => p.id !== next.id);
  return [...rest, next].sort((a, b) => b.id - a.id);
}

export function isOpen(p: ApprovalProposal): boolean {
  return p.closed_at === null;
}

/** The viewer's principal is their username; a proposal names its own. */
export function isPrincipalOn(p: ApprovalProposal, me: string): boolean {
  return p.states.some((s) => s.principal === me);
}

export function pendingForMe(
  proposals: ApprovalProposal[],
  me: string | null | undefined,
): number {
  if (me === null || me === undefined || me === "") return 0;
  return proposals.filter(
    (p) =>
      isOpen(p) &&
      p.states.some((s) => s.principal === me && s.state === "pending"),
  ).length;
}

/** Why an answer cannot be sent yet, or null when it can. */
export function actBlockReason(
  action: ApprovalAction,
  remarks: string,
): string | null {
  if (remarks.length > MAX_REMARKS_CHARS) {
    return `Remarks are limited to ${MAX_REMARKS_CHARS} characters.`;
  }
  if (action !== "approve" && remarks.trim() === "") {
    return action === "deny"
      ? "Deny needs remarks: say why."
      : "Rework needs remarks: say what to change.";
  }
  return null;
}

export const useApprovals = create<ApprovalsState>()((set, get) => ({
  status: "idle",
  detail: null,
  proposals: [],
  truncated: false,

  refresh: async () => {
    if (get().status === "idle") set({ status: "loading" });
    try {
      const body = await approvalList();
      const held = new Map(get().proposals.map((p) => [p.id, p]));
      set({
        status: "ready",
        detail: null,
        proposals: body.proposals
          .map((p) => {
            const stored = held.get(p.id);
            return stored !== undefined && isOlder(p, stored) ? stored : p;
          })
          .sort((a, b) => b.id - a.id),
        truncated: body.truncated,
      });
    } catch (e) {
      const detail =
        e instanceof ApiError ? e.detail : "Could not load approvals";
      set({
        status: e instanceof ApiError && e.status === 503 ? "unavailable" : "error",
        detail,
        proposals: [],
        truncated: false,
      });
    }
  },

  fetchOne: async (id) => {
    const { proposal } = await approvalGet(id);
    set({ proposals: upsertProposal(get().proposals, proposal) });
  },

  act: async (id, action, remarks) => {
    const trimmed = remarks.trim();
    const { proposal } = await approvalAct(id, action, trimmed === "" ? null : trimmed);
    set({ proposals: upsertProposal(get().proposals, proposal) });
    return proposal;
  },

  onUpdate: (frame) => {
    // A frame implies an armed server; a store that last saw it disarmed
    // reloads the whole list rather than holding one row of it.
    if (get().status !== "ready") {
      void get().refresh();
      return;
    }
    set({ proposals: upsertProposal(get().proposals, frame.proposal) });
  },
}));
