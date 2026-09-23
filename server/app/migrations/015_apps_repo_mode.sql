-- 015_apps_repo_mode.sql — a build session says which tree it builds.
--
-- SPECS/2026-09-20-build-lane-v2-stage1-2b.md.
--
-- 'app' is every session that existed before this migration, which is the
-- truth: they all built an app tree. A 'repo' session builds the platform
-- repo instead — it has no channel of its own (it borrows the channel the
-- `/build` was typed in), spends no daily app quota, and carries the branch
-- the broker cut for it in `repo_slug` (NULL until the broker answers).

ALTER TABLE app_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'app'
                                  CHECK (mode IN ('app', 'repo'));

ALTER TABLE app_sessions ADD COLUMN repo_slug TEXT;
