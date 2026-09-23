-- 011_apps_registry.sql — the APPS registry, stage 1.
--
-- SPECS/2026-08-30-apps-tab-v1.md (confirmed by plink, #custodian seq 2211;
-- Round 12 opens stage 1: registry + migration, APPS tab group, chooser, modal
-- chat on the resident seat, stage-event consumer with no publisher yet).
--
-- What is here is the whole stage-1 schema and nothing beyond it. There is no
-- serving gate, no screenshot, no share-card, no remix copy — those are later
-- stages of the same spec, and a column added now "so it is ready" is a column
-- nobody can read the meaning of when it lands.
--
-- Two decisions worth finding here rather than reconstructing:
--
--   * The modal build chat is a REAL CHANNEL of a new type, `app_build`. It is
--     not a parallel message store: members, history, seq allocation, WS
--     fan-out, typing, the privacy filter and the membership wall are the ones
--     the house already has. That costs exactly one widened CHECK, below.
--   * `builder_bot_id` is a bots.id and deliberately NOT a foreign key, the
--     same way messages.author_id is not: an author/actor column that can name
--     either kind of member cannot carry a single REFERENCES clause, and the
--     house rule is that such columns are plain integers.

-- ---------------------------------------------------------------------------
-- channels: widen the type CHECK to include 'app_build'
--
-- SQLite cannot alter a CHECK in place -> table rebuild, per 004/005.
--
-- CRITICAL, and the reason this is not a one-liner: channels is a PARENT table
-- (channel_members and messages reference it ON DELETE CASCADE). With
-- foreign_keys ON, DROP TABLE performs an implicit DELETE FROM, which would
-- cascade-delete every message in the house. So FKs are disabled around the
-- rebuild (the sqlite.org documented procedure) and the child tables'
-- "REFERENCES channels" resolve to the renamed table afterwards. PRAGMA
-- foreign_keys is a no-op inside a transaction, hence it brackets BEGIN/COMMIT.
--
-- BACK UP THE DATABASE BEFORE APPLYING THIS MIGRATION. A rebuild of the table
-- every message hangs off is the one migration in this tree where a mistake is
-- not recoverable by re-running it.
--
-- The column list below is channels as of 008 (visibility, created_by). If a
-- later migration adds a column and this file is ever used as the template for
-- another rebuild, copy the CURRENT schema, not this one.
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys=OFF;

BEGIN;

CREATE TABLE channels_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT    NOT NULL CHECK (type IN ('main_feed', 'dm_1to1', 'text',
                                                'app_build')),
    name       TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    visibility TEXT    NOT NULL DEFAULT 'public'
                       CHECK (visibility IN ('public', 'private')),
    created_by INTEGER REFERENCES users(id)
);

INSERT INTO channels_new (id, type, name, created_at, visibility, created_by)
SELECT id, type, name, created_at, visibility, created_by FROM channels;

DROP TABLE channels;

ALTER TABLE channels_new RENAME TO channels;

-- Text-channel names are unique (partial index: main_feed/DM/app_build names
-- unaffected — an app_build channel is named after its app, and two people may
-- both have an "Untitled app").
CREATE UNIQUE INDEX idx_channels_text_name ON channels(name) WHERE type = 'text';

COMMIT;

PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------------------
-- The registry
-- ---------------------------------------------------------------------------

CREATE TABLE apps (
    -- 12 chars of lowercase base32, minted from secrets.token_bytes. NEVER
    -- sequential: an app id ends up in a URL path segment on a shared origin,
    -- and a sequential id there is an invitation to walk the neighbours.
    id             TEXT    PRIMARY KEY,
    owner_user_id  INTEGER NOT NULL REFERENCES users(id),
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    visibility     TEXT    NOT NULL DEFAULT 'private'
                           CHECK (visibility IN ('private', 'shared', 'public')),
    -- bots.id, not a foreign key — see the header.
    builder_bot_id INTEGER NOT NULL,
    -- Lineage (Amendment A edit 3). Recorded from day one because it cannot be
    -- retrofitted: once a remix exists without its parent recorded, the parent
    -- is unknowable. Remix itself is a later stage; the column is not.
    parent_app_id  TEXT    REFERENCES apps(id),
    status         TEXT    NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft', 'live', 'archived')),
    -- /srv/apps/<app-id>/ once the apps-builder seat exists. NULL in stage 1,
    -- and honestly NULL rather than a path to a directory nobody creates yet.
    repo_path      TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Who an app has been SHARED WITH. Distinct from user_apps below, and the
-- distinction is the whole point: being allowed to see an app is not the same
-- as having put it on your menu (Amendment A edit 1 — "public does not mean
-- on-your-menu"). This table answers "may they", user_apps answers "did they".
CREATE TABLE app_shares (
    app_id   TEXT    NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (app_id, user_id)
);

-- The subscription table: apps a user has added to their APPS group. The owner
-- of an app is on its menu by construction and has no row here, which is why
-- removing your own app from your own menu is refused rather than silently
-- doing nothing.
CREATE TABLE user_apps (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    app_id   TEXT    NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    added_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (user_id, app_id)
);

-- One modal build session. This row is three things at once, deliberately:
-- the quota unit (sessions started per user per UTC day, counted from
-- started_at — there is no counter table to drift), the lock (open == ended_at
-- IS NULL AND locked_until > now, pushed out by the modal's heartbeat), and the
-- join between an app and the channel its chat lives in.
CREATE TABLE app_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id         TEXT    NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    builder_bot_id INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    started_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- RULED by plink at Round 12: 15 minutes, pushed out by each heartbeat. A
    -- lock whose locked_until is in the past is CLEAR, and its session counts as
    -- ended to every reader — there is no sweeper and there must not be one, or
    -- correctness would depend on a timer having run.
    locked_until   TEXT    NOT NULL,
    ended_at       TEXT,
    -- The last stage this session reached; NULL until the first stage event.
    stage          TEXT    CHECK (stage IN ('scoped', 'scaffolded',
                                            'files_written', 'deployed', 'live'))
);

-- The stage stream, persisted so a modal reopened after a reload shows the
-- stages that already happened instead of an empty bar. Stage vocabulary is
-- FIXED at these five (Amendment A edit 4) and there is no percent anywhere:
-- a builder's estimate of its own progress is self-report and is never rendered
-- as a number.
CREATE TABLE app_stage_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    stage      TEXT    NOT NULL CHECK (stage IN ('scoped', 'scaffolded',
                                                 'files_written', 'deployed',
                                                 'live')),
    -- A bounded JSON object (filenames, a note). Bounded at the router; the
    -- default keeps every reader's json.loads total.
    detail     TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_apps_owner ON apps(owner_user_id);
-- The lock lookup: "is there an open session on this app".
CREATE INDEX idx_app_sessions_app_open ON app_sessions(app_id, ended_at);
-- The quota count: "sessions this user started since UTC midnight".
CREATE INDEX idx_app_sessions_user_started ON app_sessions(user_id, started_at);
CREATE INDEX idx_app_stage_events_session ON app_stage_events(session_id, id);
