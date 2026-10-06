-- 020_message_trace.sql — the steps a bot reports behind a message.
--
-- SPECS/2026-10-06-why-trail.md.
--
-- A JSON object {"steps": [...], "total": int}, or {} when the message carries
-- none. Bot authors only, written by the posting bot's adapter; the server
-- checks shape and size and never verifies the content.

ALTER TABLE messages ADD COLUMN trace TEXT NOT NULL DEFAULT '{}';
