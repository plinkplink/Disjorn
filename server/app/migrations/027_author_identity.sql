-- 027_author_identity.sql — who filed a backlog row, and which resident seat a
-- relayed row or plan-room comment was written for, as columns beside the label.
--
-- author_type/author_id are the authenticated actor, as on messages.
-- author_seat is the `res-<name>` seat a relay bot named, NULL when the actor
-- wrote for itself. The label columns stay: the name shown at writing time.
--
-- An older row is mapped only when its label names exactly one candidate (a
-- username, a bot name, or a seat); every other row keeps NULL, which says
-- nobody recorded it. An old relayed backlog row keeps author_type/author_id
-- NULL because the label never said which bot relayed it.
--
-- One transaction: ADD COLUMN cannot be re-run, so a crash must leave none.
BEGIN;

ALTER TABLE backlog ADD COLUMN author_type TEXT
    CHECK (author_type IS NULL OR author_type IN ('user', 'bot'));
ALTER TABLE backlog ADD COLUMN author_id INTEGER;
ALTER TABLE backlog ADD COLUMN author_seat TEXT
    CHECK (author_seat IS NULL OR author_seat GLOB 'res-[a-z]*');
ALTER TABLE card_comments ADD COLUMN author_seat TEXT
    CHECK (author_seat IS NULL OR author_seat GLOB 'res-[a-z]*');

UPDATE backlog SET author_seat = author
 WHERE length(author) <= 64
   AND author GLOB 'res-[a-z]*'
   AND author NOT GLOB '*[^a-z0-9-]*';

UPDATE backlog
   SET author_type = 'user',
       author_id = (SELECT id FROM users WHERE username = backlog.author)
 WHERE author_seat IS NULL
   AND author IN (SELECT username FROM users)
   AND author NOT IN (SELECT name FROM bots);

UPDATE backlog
   SET author_type = 'bot',
       author_id = (SELECT id FROM bots WHERE name = backlog.author)
 WHERE author_seat IS NULL
   AND author IN (SELECT name FROM bots)
   AND author NOT IN (SELECT username FROM users);

UPDATE backlog SET author_seat = NULL
 WHERE author IN (SELECT username FROM users)
    OR author IN (SELECT name FROM bots);

-- 'broker' is the shipped default of every *_RELAY_BOT_NAMES setting; a label
-- composed for any other bot stays a label.
UPDATE card_comments
   SET author_seat = replace(author_label, ' (via broker)', '')
 WHERE author_type = 'bot'
   AND author_id IN (SELECT id FROM bots WHERE name = 'broker')
   AND author_label = replace(author_label, ' (via broker)', '') || ' (via broker)'
   AND length(author_label) <= 77
   AND author_label GLOB 'res-[a-z]*'
   AND replace(author_label, ' (via broker)', '') NOT GLOB '*[^a-z0-9-]*';

COMMIT;
