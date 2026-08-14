-- 007_password_rotation.sql — first-login password rotation.
--
-- must_change_password marks an account whose current password was chosen by
-- somebody other than its holder: created by an admin via `cli.py create-user`,
-- or reset by an admin over POST /auth/users/{id}/password. While the flag is
-- set the account can still log in, but every authenticated route except
-- POST /auth/password, GET /me and POST /auth/logout answers 403, so a
-- handed-over password is only ever good for replacing itself.
--
-- Existing rows are marked too, deliberately: the complaint this closes ("there
-- is no ability to change passwords, so I have every user's password forever")
-- is not fixed until the passwords that predate this migration rotate as well.
-- `users` holds humans only — bots live in `bots` and authenticate with API
-- keys, which this migration does not touch.

ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;

UPDATE users SET must_change_password = 1;
