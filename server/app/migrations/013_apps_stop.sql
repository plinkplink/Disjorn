-- 013_apps_stop.sql — a build session records that the user asked for the
-- running turn to stop.
--
-- SPECS/2026-09-08-apps-stop-turn.md (confirmed by plink, #custodian seq
-- 2444; builder-seat slice (iv)).
--
-- One column. `stop_requested_at` is set by POST /apps/sessions/{id}/stop, and
-- by /end when a turn is running (ending the session stops the turn first);
-- it is read by the broker's reaper on each poll of harness-view, which is
-- what turns the click into `systemctl stop` on the turn's unit; and it is
-- reset to NULL by the stage endpoint on the turn's terminal event, so a
-- later turn of the same session does not inherit a stale request.
--
-- NULL is "nobody asked". A session created before this migration reads NULL,
-- which is the truth.

ALTER TABLE app_sessions ADD COLUMN stop_requested_at TEXT NULL;
