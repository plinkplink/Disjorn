-- 005_text_channels.sql — named text channels.
--
-- channels.type gains 'text' (named, user-creatable channels; users are
-- implicit members like main_feed, bots explicit-only). SQLite cannot alter a
-- CHECK constraint in place -> table rebuild, per the 004 pattern.
--
-- CRITICAL: channels is a PARENT table (channel_members and messages
-- reference it with ON DELETE CASCADE). With foreign_keys ON, DROP TABLE
-- performs an implicit DELETE FROM which would cascade-delete every message.
-- So FKs are disabled around the rebuild (the sqlite.org documented
-- procedure); child tables' "REFERENCES channels" resolve to the renamed
-- table afterwards. PRAGMA foreign_keys is a no-op inside a transaction,
-- hence it brackets the BEGIN/COMMIT.

PRAGMA foreign_keys=OFF;

BEGIN;

CREATE TABLE channels_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT    NOT NULL CHECK (type IN ('main_feed', 'dm_1to1', 'text')),
    name       TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO channels_new (id, type, name, created_at)
SELECT id, type, name, created_at FROM channels;

DROP TABLE channels;

ALTER TABLE channels_new RENAME TO channels;

-- Text-channel names are unique (partial index: main_feed/DM names unaffected).
CREATE UNIQUE INDEX idx_channels_text_name ON channels(name) WHERE type = 'text';

COMMIT;

PRAGMA foreign_keys=ON;
