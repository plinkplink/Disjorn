-- WP8: cache for link unfurls (services/unfurl.py). TTL enforced in code
-- (rows older than 7 days are refetched and upserted in place).
CREATE TABLE IF NOT EXISTS unfurl_cache (
    url         TEXT PRIMARY KEY,
    title       TEXT,
    description TEXT,
    image_url   TEXT,
    fetched_at  TEXT NOT NULL
);
