-- 010_backlog_status_verbs.sql — the backlog row's status gains a vocabulary
-- and a signature.
--
-- SPECS/2026-08-23-plan-room-phase2-slice-a.md (confirmed by plink,
-- #custodian seq 1625).
--
-- BACK UP THE DATABASE BEFORE APPLYING THIS. It is a table rebuild, not an
-- ALTER: SQLite cannot widen a CHECK constraint in place, so the only way to
-- add 'duplicate' to the allowed set is create-copy-drop-rename. The house rule
-- for a schema change is `sqlite3 disjorn.db ".backup ..."` first
-- (deploy/README-DEPLOY.md §7, Architecture.md §"Backup" — a raw file copy of a
-- live WAL database is not a backup). Every other migration here is additive
-- and forgiving; this one moves every row.
--
-- 1. `duplicate` joins the CHECK. It is the honest word for a row filed twice
--    by UI error. Backlog row 3 took `rejected` on 2026-08-23 for want of it,
--    which is a lossy record: "we decided not to" and "this is the same request
--    again" are different facts and the table could only say the first. A
--    status value with no writer is dead vocabulary, so this ships with the
--    `/backlog duplicate <id>` verb, not ahead of it.
--
-- 2. `status_by_type` / `status_by_id` / `status_at` — WHO changed the status
--    and WHEN, as DATA COLUMNS, not prose. Backlog #5's principle applied to
--    this write path from its first day rather than retrofitted onto it later.
--    The shape is deliberately `messages.author_type` / `author_id`
--    (001_init.sql:48) — the house already has one way to say "a user or a bot
--    did this", and a second one would be a second thing to keep in sync.
--    `backlog.author` above is already prose (a display label frozen at filing
--    time); this migration must not add a second prose channel while the table
--    happens to be open.
--
--    All three are NULLABLE and backfill to NULL, which is the truthful value:
--    rows that predate this migration were filed before anything recorded who
--    triaged them, and inventing an attribution for them would be worse than
--    admitting there is none.
--
-- The rebuild preserves ids exactly (an explicit-id INSERT into an AUTOINCREMENT
-- table carries sqlite_sequence forward, so the next filing continues the
-- series). Ids are load-bearing off-table: the Plan Room cards a backlog row as
-- slug `backlog-<id>`, and the reject button posts to that slug.

CREATE TABLE backlog_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    author     TEXT    NOT NULL,                  -- poster label (username / bot name)
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status     TEXT    NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'spec''d', 'built', 'rejected',
                                         'duplicate')),
    spec_ref   TEXT,                              -- nullable; the spec SLUG, never a path
    -- Typed attribution for the last status change. NULL together, or set
    -- together: a `who` with no `when` is half a record.
    status_by_type TEXT CHECK (status_by_type IS NULL
                               OR status_by_type IN ('user', 'bot')),
    status_by_id   INTEGER,
    status_at      TEXT
);

INSERT INTO backlog_new (id, text, author, created_at, status, spec_ref)
SELECT id, text, author, created_at, status, spec_ref FROM backlog;

DROP TABLE backlog;

ALTER TABLE backlog_new RENAME TO backlog;
