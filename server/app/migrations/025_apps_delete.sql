-- 025_apps_delete.sql — an app can be deleted, and its row stays.
--
-- SPECS/2026-11-11-apps-delete.md.
--
-- `deleted_at` NULL is every app that exists. A deleted row is a tombstone:
-- no list, card or grant reads it, its id is never minted again, and a remix
-- made from it keeps a parent_app_id that still names a row.
--
-- `deleted_builds` is how many of the owner's builds on the day of the delete
-- were sessions of this app. Those session rows go with the build chat, and
-- the daily meter still has to count them.

--
-- `purged_at` is set once the host helper has removed the app's files and
-- its build chats are gone. A row deleted but not purged is a delete that
-- failed part way; its owner is still shown it, to finish.

ALTER TABLE apps ADD COLUMN deleted_at TEXT;
ALTER TABLE apps ADD COLUMN deleted_builds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE apps ADD COLUMN purged_at TEXT;
