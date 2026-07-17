-- 002_notify_prefs.sql — WP7: per-user notification preference.
-- notify_all_main: when truthy, the user gets a Web Push for EVERY main-feed
-- message (not just DMs and @mentions). Off by default.

ALTER TABLE users ADD COLUMN notify_all_main INTEGER NOT NULL DEFAULT 0;
