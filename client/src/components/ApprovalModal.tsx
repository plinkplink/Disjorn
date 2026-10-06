/* One approval proposal: its text, every principal's answer, and the viewer's
   own Approve / Rework / Deny while it is open. The server derives who is
   answering, so this modal never names a principal; its refusals show verbatim. */

import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

import { ApiError } from "../api";
import {
  MAX_REMARKS_CHARS,
  actBlockReason,
  canAnswer,
  isOpen,
  isPrincipalOn,
  useApprovals,
  type ApprovalsStatus,
} from "../stores/approvals";
import type {
  ApprovalAction,
  ApprovalDecision,
  ApprovalProposal,
  ApprovalState,
  User,
} from "../types";
import { Markdown } from "./Markdown";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function decisionLabel(d: ApprovalDecision): string {
  return d === "pending" ? "open" : d;
}

export function stateLabel(s: ApprovalState): string {
  return s === "approve" ? "approved" : s === "deny" ? "denied" : s;
}

export function fullTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

export function age(iso: string, now: number = Date.now()): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const minutes = Math.max(0, Math.floor((now - t) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Esc closes, Tab cycles inside the dialog, focus returns on close. */
function useDialogFocus(ref: RefObject<HTMLElement>, onClose: () => void) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeRef.current();
        return;
      }
      const root = ref.current;
      if (e.key !== "Tab" || root === null) return;
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
      const first = items[0];
      const last = items[items.length - 1];
      if (first === undefined || last === undefined) {
        e.preventDefault();
        return;
      }
      const active = document.activeElement;
      if (!root.contains(active) || (e.shiftKey ? active === first || active === root : active === last)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [ref]);
}

function StateTable({ proposal }: { proposal: ApprovalProposal }) {
  return (
    <table className="approval-states">
      <thead>
        <tr>
          <th>Principal</th>
          <th>State</th>
          <th>Remarks</th>
          <th>Acted</th>
        </tr>
      </thead>
      <tbody>
        {proposal.states.map((s) => (
          <tr key={s.principal}>
            <td className="approval-principal">{s.principal}</td>
            <td>
              <span className={`approval-chip approval-chip-${s.state}`}>
                {stateLabel(s.state)}
              </span>
            </td>
            <td className="approval-remarks">{s.remarks ?? "—"}</td>
            <td className="approval-acted">
              {s.acted_by === null ? (
                "—"
              ) : (
                <>
                  <span title={`${s.acted_by.type} ${s.acted_by.id ?? ""}`.trim()}>
                    {s.acted_by.label}
                  </span>
                  <span className="approval-when">{fullTime(s.acted_at)}</span>
                </>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const ACTIONS: { action: ApprovalAction; label: string; className: string }[] = [
  { action: "approve", label: "Approve", className: "btn btn-primary" },
  { action: "rework", label: "Rework", className: "btn" },
  { action: "deny", label: "Deny", className: "btn btn-danger" },
];

export function AnswerBox({ proposal }: { proposal: ApprovalProposal }) {
  const [remarks, setRemarks] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = (action: ApprovalAction) => {
    if (busy || actBlockReason(action, remarks) !== null) return;
    setBusy(true);
    setError(null);
    useApprovals
      .getState()
      .act(proposal.id, action, remarks)
      .then(() => setRemarks(""))
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.detail : "That answer did not go through"),
      )
      .finally(() => setBusy(false));
  };

  const reason =
    actBlockReason("deny", remarks) === null
      ? actBlockReason("approve", remarks)
      : "Deny and Rework need remarks.";

  return (
    <div className="approval-answer">
      <label className="approval-answer-label" htmlFor="approval-remarks">
        Remarks
      </label>
      <textarea
        id="approval-remarks"
        className="plan-input approval-textarea"
        value={remarks}
        maxLength={MAX_REMARKS_CHARS}
        rows={4}
        disabled={busy}
        onChange={(e) => setRemarks(e.target.value)}
      />
      <div className="approval-answer-row">
        <span className="approval-counter">
          {remarks.length} / {MAX_REMARKS_CHARS}
        </span>
        {ACTIONS.map(({ action, label, className }) => {
          const blocked = actBlockReason(action, remarks);
          return (
            <button
              key={action}
              className={className}
              disabled={busy || blocked !== null}
              title={blocked ?? undefined}
              onClick={() => send(action)}
            >
              {label}
            </button>
          );
        })}
      </div>
      {reason !== null && <p className="approval-reason">{reason}</p>}
      {error !== null && <p className="plan-error approval-error">{error}</p>}
      <p className="plan-modal-hint">
        Answering again replaces your answer while the proposal is open.
      </p>
    </div>
  );
}

/** Why a slug has no proposal to show: the list may not have answered yet. */
function Absent({
  slug,
  status,
  detail,
}: {
  slug: string;
  status: ApprovalsStatus;
  detail: string | null;
}) {
  if (status === "unavailable" || status === "error") {
    return (
      <p className={`approvals-off${status === "error" ? " approvals-error" : ""}`}>
        {detail}
      </p>
    );
  }
  if (status !== "ready") return <p className="approvals-empty">Loading…</p>;
  return (
    <p className="plan-modal-note">
      No approval proposal has the slug <code>{slug}</code>.
    </p>
  );
}

export function ApprovalModal({
  slug,
  status,
  detail,
  proposal,
  me,
  hasSpecCard,
  onOpenCard,
  onClose,
}: {
  slug: string;
  status: ApprovalsStatus;
  detail: string | null;
  proposal: ApprovalProposal | null;
  me: Pick<User, "username" | "is_admin"> | null;
  hasSpecCard: boolean;
  onOpenCard: () => void;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef, onClose);

  const open = proposal !== null && isOpen(proposal);
  const mine = proposal !== null && me !== null && isPrincipalOn(proposal, me.username);
  const answerable = proposal !== null && canAnswer(proposal, me);

  return (
    <div className="modal-backdrop approval-backdrop" onClick={onClose}>
      <div
        ref={dialogRef}
        className="plan-modal approval-modal"
        role="dialog"
        aria-modal="true"
        aria-label={proposal?.title ?? slug}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="plan-modal-head">
          <h2>{proposal?.title ?? slug}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {proposal === null ? (
          <Absent slug={slug} status={status} detail={detail} />
        ) : (
          <>
            <p className="approval-byline">
              <code>{proposal.slug}</code> · filed by {proposal.created_by.label}{" "}
              <span title={fullTime(proposal.created_at)}>
                {age(proposal.created_at)}
              </span>{" "}
              <span className={`approval-decision approval-decision-${proposal.decision}`}>
                {decisionLabel(proposal.decision)}
              </span>
            </p>
            {hasSpecCard && (
              <button className="approval-spec-link" onClick={onOpenCard}>
                Open the spec's Plan Room card
              </button>
            )}

            <div className="approval-text">
              <Markdown content={proposal.text} />
            </div>

            <h3 className="plan-modal-sub">Answers</h3>
            <StateTable proposal={proposal} />

            {open && answerable && <AnswerBox key={proposal.id} proposal={proposal} />}
            {open && mine && !answerable && (
              <p className="plan-modal-hint">
                Only admins can answer approval proposals here, so this record is
                read-only for you.
              </p>
            )}
            {open && !mine && (
              <p className="plan-modal-hint">
                You are not a principal on this proposal, so there is nothing for
                you to answer.
              </p>
            )}
            {!open && (
              <p className="plan-modal-hint">
                Closed {fullTime(proposal.closed_at)} as{" "}
                {decisionLabel(proposal.decision)}. The record stands as shown.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
