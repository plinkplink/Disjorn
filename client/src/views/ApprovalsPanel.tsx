/* The Plan Room's Approvals tab: open proposals first, then closed ones folded
   away, each opening the approval modal at #/planroom/approvals/<slug>. */

import { useEffect, useState } from "react";

import {
  ApprovalModal,
  age,
  decisionLabel,
  fullTime,
  stateLabel,
} from "../components/ApprovalModal";
import {
  planRoomRouteFromHash,
  writePlanRoomHash,
  type PlanRoomRoute,
} from "../hashRoute";
import { isOpen, pendingForMe, useApprovals } from "../stores/approvals";
import { useSession } from "../stores/session";
import type { ApprovalProposal } from "../types";

/** The Plan Room's tab and open proposal, kept in the hash. */
export function usePlanRoomRoute(): [PlanRoomRoute, (r: PlanRoomRoute) => void] {
  const [route, setRoute] = useState<PlanRoomRoute>(
    () => planRoomRouteFromHash() ?? { tab: "board", slug: null },
  );
  useEffect(() => {
    const onHash = () => {
      const next = planRoomRouteFromHash();
      if (next !== null) setRoute(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = (next: PlanRoomRoute) => {
    setRoute(next);
    writePlanRoomHash(next);
  };
  return [route, go];
}

export function usePendingForMe(): number {
  const me = useSession((s) => s.user?.username);
  return useApprovals((s) => pendingForMe(s.proposals, me));
}

export function PlanRoomTabs({
  tab,
  onSelect,
}: {
  tab: PlanRoomRoute["tab"];
  onSelect: (tab: PlanRoomRoute["tab"]) => void;
}) {
  const pending = usePendingForMe();
  return (
    <div className="plan-tabs" role="tablist">
      <button
        role="tab"
        aria-selected={tab === "board"}
        className={`plan-tab${tab === "board" ? " active" : ""}`}
        onClick={() => onSelect("board")}
      >
        Board
      </button>
      <button
        role="tab"
        aria-selected={tab === "approvals"}
        className={`plan-tab${tab === "approvals" ? " active" : ""}`}
        onClick={() => onSelect("approvals")}
      >
        Approvals
        {pending > 0 && (
          <span
            className="unread-badge plan-tab-badge"
            title={`${pending} waiting on your answer`}
          >
            {pending}
          </span>
        )}
      </button>
    </div>
  );
}

function Row({
  proposal,
  onOpen,
}: {
  proposal: ApprovalProposal;
  onOpen: (slug: string) => void;
}) {
  return (
    <button className="approval-row" onClick={() => onOpen(proposal.slug)}>
      <span className="approval-row-title">{proposal.title}</span>
      <span className="approval-row-meta">
        <code>{proposal.slug}</code>
        <span>{proposal.created_by.label}</span>
        <span title={fullTime(proposal.created_at)}>{age(proposal.created_at)}</span>
        <span className={`approval-decision approval-decision-${proposal.decision}`}>
          {decisionLabel(proposal.decision)}
        </span>
      </span>
      <span className="approval-row-chips">
        {proposal.states.map((s) => (
          <span
            key={s.principal}
            className={`approval-chip approval-chip-${s.state}`}
            title={
              s.acted_at === null
                ? `${s.principal}: not answered yet`
                : `${s.principal}: ${stateLabel(s.state)} ${fullTime(s.acted_at)}`
            }
          >
            {s.principal}
          </span>
        ))}
      </span>
    </button>
  );
}

export function ApprovalsPanel({
  slug,
  onOpen,
  hasSpecCard,
  onOpenCard,
}: {
  slug: string | null;
  onOpen: (slug: string | null) => void;
  hasSpecCard: (slug: string) => boolean;
  onOpenCard: (slug: string) => void;
}) {
  const status = useApprovals((s) => s.status);
  const detail = useApprovals((s) => s.detail);
  const proposals = useApprovals((s) => s.proposals);
  const truncated = useApprovals((s) => s.truncated);
  const me = useSession((s) => s.user);
  const [showClosed, setShowClosed] = useState(false);

  useEffect(() => {
    void useApprovals.getState().refresh();
  }, []);

  const openOnes = proposals.filter(isOpen);
  const closed = proposals.filter((p) => !isOpen(p));
  const current = slug === null ? null : proposals.find((p) => p.slug === slug) ?? null;

  useEffect(() => {
    if (current !== null) void useApprovals.getState().fetchOne(current.id).catch(() => {});
    // Only on opening: live frames keep it current after that.
  }, [current?.id]);

  // A deep link opens before the list answers, so the modal says why it has
  // no proposal yet rather than "not found".
  const modal = slug !== null && (
    <ApprovalModal
      slug={slug}
      status={status}
      detail={detail}
      proposal={current}
      me={me}
      hasSpecCard={hasSpecCard(slug)}
      onOpenCard={() => onOpenCard(slug)}
      onClose={() => onOpen(null)}
    />
  );

  if (status === "unavailable" || status === "error") {
    return (
      <section className="approvals">
        <p className={`approvals-off${status === "error" ? " approvals-error" : ""}`}>
          {detail}
        </p>
        {modal}
      </section>
    );
  }

  return (
    <section className="approvals">
      {status !== "ready" ? (
        <p className="approvals-empty">Loading…</p>
      ) : (
        <>
          <h2 className="approvals-head">
            Open <span className="plan-count">{openOnes.length}</span>
          </h2>
          {openOnes.length === 0 ? (
            <p className="approvals-empty">No open proposals.</p>
          ) : (
            <div className="approvals-list">
              {openOnes.map((p) => (
                <Row key={p.id} proposal={p} onOpen={onOpen} />
              ))}
            </div>
          )}

          <button
            className="plan-archived-toggle"
            onClick={() => setShowClosed((v) => !v)}
          >
            {showClosed ? "▾" : "▸"} Closed ({closed.length})
          </button>
          {showClosed && (
            <div className="approvals-list approvals-closed">
              {closed.length === 0 && <p className="approvals-empty">None yet.</p>}
              {closed.map((p) => (
                <Row key={p.id} proposal={p} onOpen={onOpen} />
              ))}
            </div>
          )}
          {truncated && (
            <p className="plan-modal-hint">
              Showing the {proposals.length} most recent proposals.
            </p>
          )}
        </>
      )}

      {modal}
    </section>
  );
}
