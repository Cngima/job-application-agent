-- Stage 2: sources, jobs and the ATS lookup cache.

CREATE TABLE sources (
    name            TEXT PRIMARY KEY,
    adapter         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    last_run_at     TEXT,
    last_status     TEXT,          -- ok | error | skipped
    last_listed     INTEGER,
    last_new        INTEGER,
    last_error      TEXT
);

CREATE TABLE jobs (
    id                  TEXT PRIMARY KEY,           -- sha1(canonical_url)[:16]
    canonical_url       TEXT NOT NULL UNIQUE,
    dedupe_key          TEXT NOT NULL,              -- normalised title|company
    source              TEXT NOT NULL,
    source_kind         TEXT NOT NULL,
    text_origin         TEXT NOT NULL,              -- employer | aggregator | community | pasted
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    url                 TEXT NOT NULL,
    employer_url        TEXT,
    apply_url           TEXT,
    external_id         TEXT,
    location_text       TEXT,
    aggregator_location TEXT,
    description         TEXT NOT NULL DEFAULT '',
    employment_type     TEXT,
    remote              INTEGER,
    salary_min          REAL,
    salary_max          REAL,
    salary_currency     TEXT,
    salary_period       TEXT,
    salary_text         TEXT,
    posted_at           TEXT,
    first_seen_at       TEXT NOT NULL,
    tags                TEXT NOT NULL DEFAULT '[]', -- JSON
    flags               TEXT NOT NULL DEFAULT '[]', -- JSON
    attribution         TEXT
);
CREATE INDEX idx_jobs_dedupe ON jobs (dedupe_key);
CREATE INDEX idx_jobs_first_seen ON jobs (first_seen_at);

-- Which public ATS board a company uses, so discovery jobs can be traced to the employer's
-- own posting. ats IS NULL records a negative lookup.
CREATE TABLE ats_lookup (
    company_key TEXT PRIMARY KEY,
    ats         TEXT,
    slug        TEXT,
    checked_at  TEXT NOT NULL
);

CREATE TABLE sourcing_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    source         TEXT NOT NULL,
    listed         INTEGER NOT NULL DEFAULT 0,
    title_filtered INTEGER NOT NULL DEFAULT 0,
    duplicates     INTEGER NOT NULL DEFAULT 0,
    new_jobs       INTEGER NOT NULL DEFAULT 0,
    resolved       INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'ok',
    error          TEXT
);
