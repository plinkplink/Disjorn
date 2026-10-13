-- 018_bot_reply_modes.sql — when a bot answers, and who may DM it.
--
-- SPECS/2026-10-05-bot-reply-modes-and-bot-dms.md.
--
-- channel_members.reply_mode means something only on member_type='bot' rows:
-- 'mention' answers when its name is used (every row that exists before this
-- migration), 'always' is addressed by every user-authored message in the room.
--
-- bots.dm_open gates POST /dms {bot_id}; off until an admin flips it.

ALTER TABLE channel_members ADD COLUMN reply_mode TEXT NOT NULL DEFAULT 'mention'
    CHECK (reply_mode IN ('mention', 'always'));

ALTER TABLE bots ADD COLUMN dm_open INTEGER NOT NULL DEFAULT 0;
