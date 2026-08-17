-- 008_channel_visibility.sql — per-channel membership as an enforced wall.
--
-- channels gains two columns:
--
--   visibility  'public' (everyone in the house is a member, the behaviour
--               every channel has today) or 'private' (channel_members is the
--               wall: only listed members may read).
--   created_by  the channel's owner. Only the owner may invite or kick
--               (RULED by plink, 2026-08-12), and `channels` had no owner
--               column before this spec.
--
-- Grandfathering: visibility DEFAULTs to 'public', so every channel that
-- already exists stays exactly as readable as it was on the day this lands.
-- created_by is backfilled for existing text channels to the first admin —
-- their real creator was never recorded, and an ownerless text channel could
-- never be invited to. main_feed and DM channels have no creator and are left
-- NULL: owner-only invite is a type='text' rule.
--
-- No table rebuild (unlike 004/005): ADD COLUMN with a constant default and a
-- CHECK is in-place, and `created_by REFERENCES users(id)` is legal under
-- foreign_keys=ON because its default is NULL.

ALTER TABLE channels ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'
    CHECK (visibility IN ('public', 'private'));

ALTER TABLE channels ADD COLUMN created_by INTEGER REFERENCES users(id);

UPDATE channels
   SET created_by = (SELECT MIN(id) FROM users WHERE is_admin = 1)
 WHERE type = 'text';
