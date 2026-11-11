-- Summaries by normalised URL, engine and model (services/summarize.py), so a
-- repeat click costs no model call. The lifetime is enforced in code.
CREATE TABLE summary_cache (
    url        TEXT NOT NULL,
    engine     TEXT NOT NULL,
    model      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (url, engine, model)
);
