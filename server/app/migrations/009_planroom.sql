-- 009_planroom.sql — the Plan Room's board-native state, and ONLY that.
--
-- SPECS/2026-08-20-plan-room.md (confirmed by plink, #custodian seq 1434).
--
-- The load-bearing rule of the whole feature is that the board owns no
-- authoritative state: every card is a rendering of an artifact that already
-- exists — a SPECS/ file's Status line, a confirm seq, a gatehouse branch, a
-- backlog row, deploy provenance. Those are derived broker-side into a SQLite
-- index that is a CACHE, NEVER A SOURCE: git wins every disagreement and the
-- index rebuilds from zero. None of it is here, and none of it may ever be.
--
-- These two tables are the exception the rule needs to be useful, and they are
-- the complete list of what the board owns natively: comments, card order, the
-- blocked flag + its reason, archived. This state is AUTHORITATIVE, exists
-- nowhere else in the house, and survives every index rebuild. If it lived in
-- the cache, "rebuild from zero" would mean "delete every comment anybody
-- wrote."
--
-- KEYED ON THE SPEC SLUG, NOT ON A PATH (seq 1428 P5). slug == branch ==
-- systemd unit == sidecar key, regex-validated in three programs, and
-- board.py's `mark_merged` already refuses to rename a spec file for exactly
-- this reason. Comments keyed on a movable path are orphans waiting to happen.
-- Renames are unsupported; that is a property of the house, not an omission
-- here.
--
-- There is deliberately NO foreign key to anything. A card is not a row — it
-- is derived — so there is nothing for these to reference. A comment may be
-- written against a slug whose card is not currently on the board (a spec that
-- was superseded, a keyboard card whose commit has been rewritten), and it must
-- survive to be read when the card comes back. Orphaned rows here are cheap;
-- a cascade that deleted somebody's comment because a derivation blinked is not.

CREATE TABLE card_meta (
    slug           TEXT    PRIMARY KEY,          -- the spec slug; see above
    sort_order     INTEGER,                      -- within its column; NULL = derived order
    blocked        INTEGER NOT NULL DEFAULT 0,   -- a FLAG with a reason, never a column:
    blocked_reason TEXT,                         -- a held card keeps its place so everyone
    blocked_by     TEXT,                         -- can see where it re-enters
    blocked_at     TEXT,
    archived       INTEGER NOT NULL DEFAULT 0,   -- only meaningful on a merged card
    archived_at    TEXT,
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (blocked IN (0, 1)),
    CHECK (archived IN (0, 1)),
    -- A blocked card without a reason is a card nobody can unblock, because
    -- nobody can tell what would have to change. Enforced here rather than only
    -- at the router: the broker writes through the router, but the next writer
    -- might not.
    CHECK (blocked = 0 OR (blocked_reason IS NOT NULL AND blocked_reason <> ''))
);

CREATE TABLE card_comments (
    slug         TEXT    NOT NULL,
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    author_type  TEXT    NOT NULL CHECK (author_type IN ('user', 'bot')),
    author_id    INTEGER NOT NULL,
    -- The display name at the time of writing. Denormalised on purpose: a
    -- comment is a record of who said what when, and it must not silently
    -- re-attribute itself if a display name changes later.
    --
    -- For a comment filed through the broker's `board-comment` verb this is the
    -- CALLING RESIDENT's name, stamped broker-side from SO_PEERCRED and never
    -- taken from the caller's arguments — the same identity rule `file-proposal`
    -- has always run under. author_type/author_id still say `bot`/<broker>,
    -- because the broker's bot identity is the wall; the label is its
    -- attestation of who asked.
    author_label TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_card_comments_slug ON card_comments(slug, id);
