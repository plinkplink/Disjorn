-- 004_media_nullable.sql (WP6) — attachments rework for the staged-upload flow.
--
-- 1. message_id becomes NULLABLE: uploads are staged (message_id NULL) until the
--    client links them to a message via POST /attachments/claim (or uploads with
--    an explicit message_id). SQLite cannot drop NOT NULL in place -> rebuild.
-- 2. New columns:
--      display_path / thumb_path — web-friendly WebP variants (relative to DATA_DIR;
--                                  NULL when the original is served directly or the
--                                  file is not an image)
--      uploader_type / uploader_id — who uploaded it; claim is restricted to the
--                                    uploader (prevents claiming others' staged files)
--      created_at                  — upload timestamp (staged-row GC candidate key)

CREATE TABLE attachments_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    file_path         TEXT    NOT NULL,
    original_filename TEXT    NOT NULL,
    mime_type         TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL,
    width             INTEGER,
    height            INTEGER,
    display_path      TEXT,
    thumb_path        TEXT,
    uploader_type     TEXT    CHECK (uploader_type IN ('user', 'bot')),
    uploader_id       INTEGER,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO attachments_new
    (id, message_id, file_path, original_filename, mime_type, size_bytes, width, height)
SELECT id, message_id, file_path, original_filename, mime_type, size_bytes, width, height
FROM attachments;

DROP TABLE attachments;

ALTER TABLE attachments_new RENAME TO attachments;

CREATE INDEX idx_attachments_message ON attachments(message_id);
