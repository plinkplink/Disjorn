-- 022_message_mentions.sql — which bots a message addressed with an explicit @.
--
-- SPECS/2026-11-11-at-bots-mention.md.
--
-- A JSON array of bot ids, resolved by the server when the message is written
-- and never taken from the request. Every row that exists before this
-- migration reads []: old messages are not re-read.

ALTER TABLE messages ADD COLUMN mentioned_bot_ids TEXT NOT NULL DEFAULT '[]';
