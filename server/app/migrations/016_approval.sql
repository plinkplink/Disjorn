-- 016_approval.sql — the approval object: ONE record per proposal, answered
-- from the keyboard and from chat alike.
--
-- SPECS/2026-08-26-approval-object-and-resident-write-verbs.md (confirmed by
-- plink, #custodian seq 2022).
--
-- The whole point is that there is one state of record. plink answers from a
-- client modal; residents answer through the broker's `approval-act` verb,
-- which reaches the same endpoint under the broker's own bot identity. Two
-- surfaces, one row — a second store for "what the residents said" would be
-- the forked truth this object exists to prevent.
--
-- The surface ships OFF (APPROVAL_ENABLED, config.py). Arming it is a
-- witnessed plink config change, not something a migration or a build does.
-- These tables exist unarmed and empty until then.
--
-- NOTHING HERE STORES A DECISION. Approved / denied / rework / pending is
-- DERIVED from the state rows on every read (routers/approval.py `_decision`),
-- so there is no column anybody can set to disagree with the principals'
-- answers. `closed_at` is the one consequence that is written, and it is
-- written from the same derivation in the same statement as the act.

CREATE TABLE approval_proposal (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    -- A stable handle — a spec slug, or any short kebab handle. UNIQUE because
    -- it is what a resident types at the broker to name a proposal; two rows
    -- answering to one handle is an ambiguity the verb cannot resolve.
    slug  TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    text  TEXT NOT NULL,
    -- Typed attribution plus a denormalised label, exactly as card_comments
    -- (009) does it: the label is the display name AT THE TIME OF WRITING and
    -- must not silently re-attribute itself later. For a proposal filed through
    -- the broker the label is the calling resident's, stamped broker-side from
    -- SO_PEERCRED; created_by_type/id still say bot/<broker>, because the bot
    -- identity is the wall and the label is its attestation of who asked.
    created_by_type  TEXT    NOT NULL CHECK (created_by_type IN ('user', 'bot')),
    created_by_id    INTEGER NOT NULL,
    created_by_label TEXT    NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Set exactly when the derived decision reaches approved or denied. There
    -- is no close endpoint; closing is a consequence, never a separate act.
    closed_at  TEXT
);

-- One row per (proposal, principal), written for EVERY configured principal at
-- proposal creation. An unanswered principal is therefore a `pending` row, not
-- an absent one: a missing row and a pending one must not read alike, and a
-- reader counting answers must not have to guess whether silence means "has
-- not looked" or "is not being asked".
CREATE TABLE approval_state (
    proposal_id INTEGER NOT NULL REFERENCES approval_proposal(id) ON DELETE CASCADE,
    principal   TEXT    NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'approve', 'deny', 'rework')),
    remarks     TEXT,
    -- Typed attribution again, never prose-only. All NULL while pending: a bot
    -- acting for a resident sets acted_by_type='bot' and carries the resident's
    -- name in acted_by_label.
    acted_by_type  TEXT CHECK (acted_by_type IS NULL
                               OR acted_by_type IN ('user', 'bot')),
    acted_by_id    INTEGER,
    acted_by_label TEXT,
    acted_at       TEXT,
    PRIMARY KEY (proposal_id, principal),
    -- An answer with no time is half a record, and `closed_at` is derived from
    -- these rows: an untimed answer would close a proposal at no moment.
    CHECK (state = 'pending' OR acted_at IS NOT NULL)
);
