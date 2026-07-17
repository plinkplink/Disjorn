-- 001_init.sql — full core schema per Architecture.md §4.1
-- Conventions: integer autoincrement PKs, UTC ISO-8601 text timestamps,
-- JSON stored as TEXT, soft delete via deleted_at.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL,
    avatar_path   TEXT,
    status        TEXT    NOT NULL DEFAULT 'offline'
                  CHECK (status IN ('online', 'idle', 'dnd', 'offline')),
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT    NOT NULL
);

CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT    NOT NULL CHECK (type IN ('main_feed', 'dm_1to1')),
    name       TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE channel_members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    member_type   TEXT    NOT NULL CHECK (member_type IN ('user', 'bot')),
    member_id     INTEGER NOT NULL,
    last_read_seq INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel_id, member_type, member_id)
);

CREATE INDEX idx_channel_members_member ON channel_members(member_type, member_id);

CREATE TABLE messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,               -- per-channel monotonic (allocated in WP4)
    author_type   TEXT    NOT NULL CHECK (author_type IN ('user', 'bot')),
    author_id     INTEGER NOT NULL,
    content       TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    edited_at     TEXT,                           -- nullable
    deleted_at    TEXT,                           -- nullable; soft delete
    reply_to_id   INTEGER REFERENCES messages(id),
    privacy_flags TEXT    NOT NULL DEFAULT '{}',  -- JSON object: {"secret": true, ...}
    emote_refs    TEXT    NOT NULL DEFAULT '[]',  -- JSON array (bot chibi refs)
    UNIQUE (channel_id, seq)
);

CREATE INDEX idx_messages_reply_to ON messages(reply_to_id) WHERE reply_to_id IS NOT NULL;

CREATE TABLE attachments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    file_path         TEXT    NOT NULL,
    original_filename TEXT    NOT NULL,
    mime_type         TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL,
    width             INTEGER,
    height            INTEGER
);

CREATE INDEX idx_attachments_message ON attachments(message_id);

CREATE TABLE bots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    api_key_hash TEXT    NOT NULL,
    avatar_path  TEXT,
    chibi_pack   TEXT,                            -- nullable path to chibi pack dir
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint   TEXT    NOT NULL UNIQUE,
    keys_json  TEXT    NOT NULL,                  -- JSON: {"p256dh": ..., "auth": ...}
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_push_subscriptions_user ON push_subscriptions(user_id);

-- Full-text search over message content (external-content FTS5, trigger-maintained).
-- Soft-deleted messages stay indexed; readers must join messages and filter
-- deleted_at IS NULL (WP4 search does this).
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER messages_fts_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;
