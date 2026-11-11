-- 026_bot_file_posts.sql — the answer each keyed bot file post got, so a
-- retry of the same post is answered from here instead of posting again.
--
-- SPECS/2026-10-15-channel-files-for-bots.md.

CREATE TABLE bot_file_posts (
    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    idempotency_key TEXT    NOT NULL,
    attachment_id   INTEGER NOT NULL,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (bot_id, idempotency_key)
);
