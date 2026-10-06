-- 019_channel_deletions.sql — a durable record that a channel was deleted.
--
-- One row per DELETE /channels/{id}, written in the same transaction as the
-- delete. It says that the channel existed, who removed it and when, and how
-- big it was. It never holds message contents: deletion is a privacy act.
--
-- No foreign keys: channel_id names a row that is gone by design, and the
-- record has to outlive any user or bot it mentions. deleted_by_type is
-- 'user' or 'bot' like every other actor pair in the schema.
--
-- channel_name is NULL for a dm_1to1 so the record cannot say who talked to
-- whom.

CREATE TABLE channel_deletions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id         INTEGER NOT NULL,
    channel_type       TEXT    NOT NULL,
    channel_name       TEXT,
    visibility         TEXT    NOT NULL,
    created_by         INTEGER,
    channel_created_at TEXT    NOT NULL,
    deleted_by_type    TEXT    NOT NULL CHECK (deleted_by_type IN ('user', 'bot')),
    deleted_by_id      INTEGER NOT NULL,
    deleted_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    message_count      INTEGER NOT NULL,
    member_count       INTEGER NOT NULL
);
