-- 006_backlog.sql — feature-request backlog + a system author for server-rendered replies.
--
-- WP-L2 (slash-command framework + /backlog). Users file requests from any
-- channel via `/backlog <text>`; residents triage the table later via the SDK.
-- The command's own server-rendered replies (the `/backlog` listing, "filed"
-- acks) are posted as ordinary chat messages so absent users and bots see them
-- async — those messages need an author. There is no dedicated system-author
-- concept in the schema (messages.author_type is only 'user'|'bot'), so we seed
-- a minimal 'system' bot to own them. Its api_key_hash is a sentinel that no
-- real key can hash to (hash_api_key always yields 64 hex chars), so it can
-- never authenticate — it exists purely as a message author.

CREATE TABLE backlog (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    author     TEXT    NOT NULL,                  -- poster label (username / bot name)
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status     TEXT    NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'spec''d', 'built', 'rejected')),
    spec_ref   TEXT                               -- nullable; set when triaged into a spec
);

INSERT INTO bots (name, api_key_hash)
VALUES ('system', 'system-no-login-not-a-sha256-hash');
