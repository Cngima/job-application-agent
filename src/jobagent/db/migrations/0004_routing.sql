-- Stage 3: AI call log and routing decisions.

CREATE TABLE ai_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    purpose        TEXT NOT NULL,          -- e.g. route, eval, ping
    provider       TEXT NOT NULL,          -- claude_code | ollama | anthropic_api | fake
    model          TEXT,
    latency_ms     INTEGER,
    input_tokens   INTEGER,                -- where the provider reports them
    output_tokens  INTEGER,
    cost_usd       REAL,                   -- only for the paid API provider
    items          INTEGER NOT NULL DEFAULT 1,  -- jobs in the batch
    attempt        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL,          -- ok | invalid | error | paused
    error          TEXT,
    prompt_version TEXT
);
CREATE INDEX idx_ai_calls_started ON ai_calls (started_at);

-- One row per routing decision; the latest row per job is the current one.
CREATE TABLE decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               TEXT NOT NULL REFERENCES jobs(id),
    decided_at           TEXT NOT NULL,
    route                TEXT NOT NULL,    -- APPLY | REVIEW | SKIP | CHECK_ELIGIBILITY | LINKEDIN_LEAD
    stage                TEXT NOT NULL,    -- rules | ai | ai_failed | lead
    eligibility_verdict  TEXT,             -- KENYA_CONFIRMED | AFRICA_OR_WORLDWIDE_NO_EXCLUSION | UNCLEAR | EXCLUDED
    quoted_location_text TEXT,
    function_match       TEXT,
    seniority_match      TEXT,
    workplace            TEXT,
    hours_requirement    TEXT,             -- JSON
    pay_estimate         TEXT,             -- JSON or NULL
    priority             INTEGER,
    rule_hits            TEXT NOT NULL DEFAULT '[]',  -- JSON list of {rule, outcome, detail}
    reasons              TEXT NOT NULL DEFAULT '[]',  -- JSON list
    ai_route             TEXT,             -- what the AI said before policy checks
    provider             TEXT,
    model                TEXT,
    prompt_version       TEXT
);
CREATE INDEX idx_decisions_job ON decisions (job_id, id);
