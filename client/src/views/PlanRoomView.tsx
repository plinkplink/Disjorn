/* The Plan Room (SPECS/2026-08-20-plan-room.md, Phase I) — hash #/planroom.

   Columns left to right, cards inside them, a modal for one card, a table for
   Archived, on a blueprint sheet.

   THE RULE THIS VIEW IS BUILT AROUND: the board owns no authoritative state.
   Every card is a rendering of an artifact that already exists — a SPECS/
   file's Status line, a confirm seq, a gatehouse branch, a backlog row, deploy
   provenance. So THERE IS NO DRAG-TO-COLUMN HERE, and its absence is the
   feature: a card changes columns only because reality moved. Phase II's
   write-through (confirm / witness / ratify / diff / merge) is a separate spec;
   nothing in this file should grow toward it.

   What this view can change is what the board owns: comments, order within a
   column, the blocked flag + its reason, archived. That list is complete. The
   controls are shown only to an admin, and — the house's rule, stated at every
   such site — the client hiding a control is never the wall; the server's
   refusal is.

   The header always says when the board was derived and from which mirror
   head. The board cannot go stale relative to the mirror because it is not a
   copy of it, but the mirror can lag, so staleness is declared, not denied. */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  planArchive,
  planBoard,
  planCard,
  planComment,
  planFlag,
  planOrder,
} from "../api";
import { useSession } from "../stores/session";
import {
  PLAN_COLUMNS,
  type PlanBoard,
  type PlanCard,
  type PlanCardDetail,
} from "../types";

const ARCHIVED = "Archived";
/* Archived is "everything that's done" and reads as a table, not as cards
   (ruled seq 1391). The other six are the board proper. */
const BOARD_COLUMNS = PLAN_COLUMNS.filter((c) => c !== ARCHIVED);

function shortSha(sha: string | null | undefined): string {
  return sha === null || sha === undefined ? "?" : sha.slice(0, 12);
}

function shortDate(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

function fullTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

/* ------------------------------------------------------------------ face */

function Face({ face }: { face: PlanBoard["face"] }) {
  if (!face.available) {
    return (
      <div className="plan-face plan-face-down">
        <strong>The board is unavailable.</strong>{" "}
        {face.unavailable_reason ?? "No reason given."}{" "}
        <span className="plan-face-hint">
          The cards are derived from the repo by the broker; nothing has been
          lost. The board comes back whole on the next rebuild.
        </span>
      </div>
    );
  }
  const badge = face.deploy?.badge ?? "unknown";
  return (
    <div className="plan-face">
      <span className="plan-face-item">
        derived <span title={fullTime(face.derived_at)}>{shortDate(face.derived_at)}</span>
      </span>
      <span className="plan-face-item">
        mirror <code>{shortSha(face.mirror_head)}</code>
      </span>
      <span
        className={`plan-badge plan-badge-${badge}`}
        title={face.deploy?.detail ?? ""}
      >
        deploy: {badge}
      </span>
      {(face.notes ?? []).map((n) => (
        <span className="plan-face-note" key={n}>
          {n}
        </span>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ card */

function Card({
  card,
  onOpen,
}: {
  card: PlanCard;
  onOpen: (slug: string) => void;
}) {
  const classes = [
    "plan-card",
    `plan-card-${card.whose_move}`,
    card.blocked ? "blocked" : "",
    card.flags.includes("lane-violation") ? "violation" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button className={classes} onClick={() => onOpen(card.slug)}>
      <span className="plan-card-title">{card.title}</span>
      <span className="plan-card-meta">
        {card.tier !== null && <span className="plan-chip">{card.tier}</span>}
        {card.review_owner !== null && (
          <span className="plan-chip">review {card.review_owner}</span>
        )}
        {card.confirm_seq !== null && (
          /* seq-hover already exists in MessageList; here the seq is shown as
             the same kind of monospace chip so the two read as one idea. */
          <span
            className="plan-chip plan-seq"
            title={`Confirmed in #custodian at seq ${card.confirm_seq}`}
          >
            seq {card.confirm_seq}
          </span>
        )}
        {card.deploy !== null && (
          <span className={`plan-badge plan-badge-${card.deploy.badge}`}>
            {card.deploy.badge}
          </span>
        )}
        {card.flags.map((f) => (
          <span className="plan-chip plan-flag" key={f}>
            {f}
          </span>
        ))}
        {card.comment_count > 0 && (
          <span className="plan-chip">{card.comment_count} 💬</span>
        )}
      </span>
      {card.blocked && (
        <span className="plan-card-blocked">
          BLOCKED — {card.blocked_reason ?? "no reason given"}
        </span>
      )}
      <span className="plan-card-where">{card.spec_path ?? card.where}</span>
    </button>
  );
}

/* ---------------------------------------------------------------- column */

function Column({
  name,
  blurb,
  cards,
  onOpen,
}: {
  name: string;
  blurb: string | undefined;
  cards: PlanCard[];
  onOpen: (slug: string) => void;
}) {
  return (
    <section className="plan-column">
      <header className="plan-column-head">
        <h2>
          {name} <span className="plan-count">{cards.length}</span>
        </h2>
        {blurb !== undefined && <p className="plan-column-blurb">{blurb}</p>}
      </header>
      <div className="plan-column-body">
        {cards.length === 0 ? (
          <p className="plan-column-empty">—</p>
        ) : (
          cards.map((c) => <Card card={c} key={c.slug} onOpen={onOpen} />)
        )}
      </div>
    </section>
  );
}

/* ----------------------------------------------------------------- modal */

function CardModal({
  slug,
  isAdmin,
  onClose,
  onChanged,
}: {
  slug: string;
  isAdmin: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<PlanCardDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    void planCard(slug)
      .then((d) => {
        setDetail(d);
        setError(null);
      })
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.detail : "Could not load the card"),
      );
  }, [slug]);

  useEffect(load, [load]);

  const act = (run: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    void run()
      .then(() => {
        load();
        onChanged();
      })
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.detail : "That did not work"),
      )
      .finally(() => setBusy(false));
  };

  const card = detail?.card ?? null;
  return (
    <div className="picker-scrim" onClick={onClose}>
      <div className="plan-modal" onClick={(e) => e.stopPropagation()}>
        <header className="plan-modal-head">
          <h2>{card?.title ?? slug}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {error !== null && <p className="plan-error">{error}</p>}
        {detail?.note !== undefined && (
          <p className="plan-modal-note">{detail.note}</p>
        )}

        {card !== null && (
          <dl className="plan-modal-fields">
            <dt>Column</dt>
            <dd>{card.column}</dd>
            {card.spec_path !== null && (
              <>
                <dt>Spec</dt>
                <dd>
                  <code>{card.spec_path}</code>
                </dd>
              </>
            )}
            {card.status !== null && (
              <>
                <dt>Status</dt>
                <dd>
                  <code>{card.status}</code>
                </dd>
              </>
            )}
            {card.tier !== null && (
              <>
                <dt>Tier</dt>
                <dd>{card.tier_note ?? card.tier}</dd>
              </>
            )}
            {card.lane !== null && (
              <>
                <dt>Lane</dt>
                <dd>{card.lane}</dd>
              </>
            )}
            {card.review_owner !== null && (
              <>
                <dt>Review owner</dt>
                <dd>{card.review_owner}</dd>
              </>
            )}
            {card.builder !== null && (
              <>
                <dt>Builder</dt>
                <dd>{card.builder}</dd>
              </>
            )}
            {card.confirm_seq !== null && (
              <>
                <dt>Confirm seq</dt>
                <dd>
                  <code
                    className="plan-seq"
                    title="The #custodian message that witnessed the confirm"
                  >
                    #{card.confirm_seq}
                  </code>
                </dd>
              </>
            )}
            {(card.shas ?? []).length > 0 && (
              <>
                <dt>Commits</dt>
                <dd>
                  {(card.shas ?? []).map((s) => (
                    <code key={s}>{shortSha(s)} </code>
                  ))}
                </dd>
              </>
            )}
            {card.deploy !== null && (
              <>
                <dt>Deploy</dt>
                <dd>
                  <span className={`plan-badge plan-badge-${card.deploy.badge}`}>
                    {card.deploy.badge}
                  </span>{" "}
                  {card.deploy.detail}
                </dd>
              </>
            )}
            <dt>Opened</dt>
            <dd>{shortDate(card.opened_at) || "—"}</dd>
            <dt>Updated</dt>
            <dd>{shortDate(card.updated_at) || "—"}</dd>
            <dt>Where</dt>
            <dd>{card.where}</dd>
          </dl>
        )}

        {card !== null && card.note !== "" && (
          <p className="plan-modal-why">{card.note}</p>
        )}

        {card !== null && card.blocked && (
          <p className="plan-modal-blocked">
            BLOCKED — {card.blocked_reason ?? "no reason given"}
            {card.blocked_by !== null && <> (by {card.blocked_by})</>}
          </p>
        )}

        <h3 className="plan-modal-sub">Comments</h3>
        <ul className="plan-comments">
          {(detail?.comments ?? []).length === 0 && (
            <li className="plan-comment-empty">Nothing yet.</li>
          )}
          {(detail?.comments ?? []).map((c) => (
            <li className="plan-comment" key={c.id}>
              <span className="plan-comment-who">{c.author_label}</span>
              <span className="plan-comment-when">{shortDate(c.created_at)}</span>
              <span className="plan-comment-text">{c.text}</span>
            </li>
          ))}
        </ul>

        {/* Board-native controls only. There is deliberately no control here
            that moves a card between columns — no such endpoint exists. */}
        {isAdmin && card !== null && (
          <div className="plan-modal-actions">
            <div className="plan-action-row">
              <input
                className="plan-input"
                placeholder="Add a comment"
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                disabled={busy}
              />
              <button
                className="btn btn-primary"
                disabled={busy || comment.trim() === ""}
                onClick={() =>
                  act(() =>
                    planComment(card.slug, comment.trim()).then(() =>
                      setComment(""),
                    ),
                  )
                }
              >
                Comment
              </button>
            </div>

            <div className="plan-action-row">
              {card.blocked ? (
                <button
                  className="btn"
                  disabled={busy}
                  onClick={() => act(() => planFlag(card.slug, false))}
                >
                  Unblock
                </button>
              ) : (
                <>
                  <input
                    className="plan-input"
                    placeholder="Why is it blocked?"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    disabled={busy}
                  />
                  <button
                    className="btn btn-danger"
                    disabled={busy || reason.trim() === ""}
                    onClick={() =>
                      act(() =>
                        planFlag(card.slug, true, reason.trim()).then(() =>
                          setReason(""),
                        ),
                      )
                    }
                  >
                    Block
                  </button>
                </>
              )}
            </div>

            <div className="plan-action-row">
              <button
                className="btn"
                disabled={busy}
                onClick={() => act(() => planOrder(card.slug, 0))}
                title="Pin to the top of its column. Order within a column is the only movement this board has."
              >
                Pin to top
              </button>
              <button
                className="btn"
                disabled={busy}
                onClick={() => act(() => planOrder(card.slug, null))}
                title="Hand the card back to the derived order: whose move it is, then oldest first."
              >
                Unpin
              </button>
              {(card.column === "Merged" || card.archived) && (
                <button
                  className="btn"
                  disabled={busy}
                  onClick={() =>
                    act(() => planArchive(card.slug, !card.archived))
                  }
                >
                  {card.archived ? "Unarchive" : "Archive"}
                </button>
              )}
            </div>
            <p className="plan-modal-hint">
              Comments, order, the blocked flag and archived are everything this
              board owns. A card changes columns only because reality moved —
              a Status line changed, a confirm landed, a merge happened.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ view */

export default function PlanRoomView({ onClose }: { onClose: () => void }) {
  const me = useSession((s) => s.user);
  const isAdmin = me?.is_admin === true;

  const [board, setBoard] = useState<PlanBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);

  const load = useCallback(() => {
    void planBoard()
      .then((b) => {
        setBoard(b);
        setError(null);
      })
      .catch((e: unknown) =>
        setError(
          e instanceof ApiError ? e.detail : "Could not load the Plan Room",
        ),
      );
  }, []);

  useEffect(load, [load]);

  const filtered = useMemo(() => {
    const cards = board?.cards ?? [];
    const q = query.trim().toLowerCase();
    if (q === "") return cards;
    return cards.filter((c) =>
      [
        c.slug,
        c.title,
        c.note,
        c.lane ?? "",
        c.review_owner ?? "",
        c.builder ?? "",
        c.tier ?? "",
        c.spec_path ?? "",
        c.flags.join(" "),
      ]
        .join("\n")
        .toLowerCase()
        .includes(q),
    );
  }, [board, query]);

  const byColumn = useMemo(() => {
    const out: Record<string, PlanCard[]> = {};
    for (const c of filtered) {
      // noUncheckedIndexedAccess: a record read is `T | undefined`, so the
      // bucket is created explicitly rather than leaned on.
      const bucket = out[c.column];
      if (bucket === undefined) out[c.column] = [c];
      else bucket.push(c);
    }
    return out;
  }, [filtered]);

  const archived = byColumn[ARCHIVED] ?? [];

  return (
    <div className="plan-room">
      <header className="plan-head">
        <button className="icon-btn" onClick={onClose} aria-label="Back">
          ←
        </button>
        <h1>Plan Room</h1>
        <input
          className="plan-input plan-search"
          placeholder="Search the board"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="icon-btn" onClick={load} title="Reload the board">
          ⟳
        </button>
      </header>

      {error !== null && <p className="plan-error">{error}</p>}
      {board !== null && <Face face={board.face} />}

      <div className="plan-board">
        {BOARD_COLUMNS.map((name) => (
          <Column
            key={name}
            name={name}
            blurb={board?.face.column_blurbs?.[name]}
            cards={byColumn[name] ?? []}
            onOpen={setOpen}
          />
        ))}
      </div>

      <section className="plan-archived">
        <button
          className="plan-archived-toggle"
          onClick={() => setShowArchived((v) => !v)}
        >
          {showArchived ? "▾" : "▸"} Archived — everything that's done (
          {archived.length})
        </button>
        {showArchived && (
          <table className="plan-table">
            <thead>
              <tr>
                <th>Card</th>
                <th>Tier</th>
                <th>Review</th>
                <th>Merge</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {archived.length === 0 && (
                <tr>
                  <td colSpan={5}>Nothing archived.</td>
                </tr>
              )}
              {archived.map((c) => (
                <tr key={c.slug} onClick={() => setOpen(c.slug)}>
                  <td>{c.title}</td>
                  <td>{c.tier ?? "—"}</td>
                  <td>{c.review_owner ?? "—"}</td>
                  <td>
                    <code>{c.merge_commit ?? "—"}</code>
                  </td>
                  <td>{shortDate(c.updated_at) || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {open !== null && (
        <CardModal
          slug={open}
          isAdmin={isAdmin}
          onClose={() => setOpen(null)}
          onChanged={load}
        />
      )}
    </div>
  );
}
