-- 023_channel_files_for_bots.sql — which uploaded files a bot may read.
--
-- SPECS/2026-10-15-channel-files-for-bots.md.
--
-- files_safe        1 once an admin marks the channel; 0 for every channel
--                   that exists today, so nothing becomes readable by landing.
-- files_safe_since  when the flag last went on; NULL while it is off.
-- bot_history_ok    1 on an upload an admin exposed with --include-history,
--                   which keeps it readable across a later off/on.

ALTER TABLE channels ADD COLUMN files_safe INTEGER NOT NULL DEFAULT 0;

ALTER TABLE channels ADD COLUMN files_safe_since TEXT;

ALTER TABLE attachments ADD COLUMN bot_history_ok INTEGER NOT NULL DEFAULT 0;
