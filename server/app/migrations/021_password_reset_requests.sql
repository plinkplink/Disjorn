-- 021_password_reset_requests.sql — "ask an admin to reset it" from the login
-- page, and the record every admin password reset leaves.
--
-- A row exists only for an account that exists; the public route answers the
-- same way either way. At most one open request per account (the partial
-- unique index), so a repeated ask never stacks rows or re-notifies admins.
-- resolved_by is NULL when the reset came from the server console.

CREATE TABLE password_reset_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    note         TEXT,
    status       TEXT    NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open', 'resolved', 'dismissed')),
    requested_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at  TEXT,
    resolved_by  INTEGER
);

CREATE UNIQUE INDEX password_reset_requests_one_open
    ON password_reset_requests (user_id) WHERE status = 'open';

-- One row per admin password reset: whose account, who reset it and when,
-- never a password. No foreign keys, so the record outlives either account;
-- actor_name is a snapshot and actor_user_id is NULL from the server console.
-- seen_at is set once the account holder has been shown the reset at login.

CREATE TABLE account_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('password_reset')),
    actor_user_id INTEGER,
    actor_name    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    seen_at       TEXT
);

CREATE INDEX account_events_by_user ON account_events (user_id, seen_at);
