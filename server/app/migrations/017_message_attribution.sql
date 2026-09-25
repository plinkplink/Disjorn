-- 017_message_attribution.sql — what produced a bot message, stored beside
-- its body instead of inside it.
--
-- SPECS/2026-09-23-footer-as-attribution.md.
--
-- A JSON object {"model": str|null, "verified": bool, "summoner": str|null},
-- or {} when the message carries none. Bot authors only, set by the posting
-- bot and never derived from content. Every row that exists before this
-- migration reads {}: bodies are not rewritten, so an old in-body footer stays
-- in the body.

ALTER TABLE messages ADD COLUMN attribution TEXT NOT NULL DEFAULT '{}';
