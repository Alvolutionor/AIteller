-- src/storage/migrations/001_initial.sql
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    author TEXT,
    published_at TIMESTAMP NOT NULL,
    content TEXT,
    metadata JSON,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    batch_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extracted_content (
    url TEXT PRIMARY KEY,
    content TEXT,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS processed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id INTEGER UNIQUE REFERENCES raw_items(id),
    passed_filter BOOLEAN NOT NULL,
    filter_reason TEXT,
    score_total REAL,
    score_breakdown JSON,
    summary TEXT,
    practice_points JSON,
    tags JSON,
    related_sources JSON,
    digested BOOLEAN DEFAULT FALSE,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    digest_date TEXT NOT NULL,
    item_count INTEGER,
    status TEXT NOT NULL,
    error_message TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    total_collected INTEGER,
    total_passed INTEGER,
    status TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_items_published ON raw_items(published_at);
CREATE INDEX IF NOT EXISTS idx_raw_items_source ON raw_items(source);
CREATE INDEX IF NOT EXISTS idx_raw_items_title ON raw_items(title);
CREATE INDEX IF NOT EXISTS idx_processed_items_score ON processed_items(score_total);
CREATE INDEX IF NOT EXISTS idx_processed_items_passed ON processed_items(passed_filter);
CREATE INDEX IF NOT EXISTS idx_processed_items_digested ON processed_items(digested);
