-- 002_scoring_fts.sql
-- Add scores, summaries tables and FTS5 for search

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id INTEGER NOT NULL UNIQUE REFERENCES raw_items(id),
    passed_filter BOOLEAN NOT NULL DEFAULT 0,
    filter_reason TEXT DEFAULT '',
    category TEXT DEFAULT '',
    total_score REAL DEFAULT 0,
    score_breakdown JSON DEFAULT '{}',
    scored_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    model_used TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scores_total ON scores(total_score);
CREATE INDEX IF NOT EXISTS idx_scores_passed ON scores(passed_filter);
CREATE INDEX IF NOT EXISTS idx_scores_category ON scores(category);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id INTEGER NOT NULL REFERENCES raw_items(id),
    summary_text TEXT NOT NULL,
    subtitle_source TEXT DEFAULT '',
    model_used TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_summaries_item ON summaries(raw_item_id);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, content, author,
    content=raw_items, content_rowid=id
);

CREATE INDEX IF NOT EXISTS idx_raw_batch ON raw_items(batch_id);

ALTER TABLE collection_runs ADD COLUMN lookback_hours INTEGER;
ALTER TABLE collection_runs ADD COLUMN sources_collected JSON
