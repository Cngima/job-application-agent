-- WhatsApp inbox, LinkedIn alert emails and scam checks.

-- Why a job was flagged (scam checks), as a JSON list of plain-English reasons.
ALTER TABLE jobs ADD COLUMN risk_reasons TEXT NOT NULL DEFAULT '[]';

-- Gmail messages already read (LinkedIn job alerts). The agent never changes the mailbox,
-- so this table is how it remembers what is done.
CREATE TABLE gmail_messages (
    message_id   TEXT PRIMARY KEY,
    received_at  TEXT,
    sender       TEXT,
    processed_at TEXT NOT NULL,
    leads        INTEGER NOT NULL DEFAULT 0
);

-- WhatsApp inbox files already processed (by content hash, so a renamed copy is not re-read).
CREATE TABLE inbox_files (
    content_hash TEXT PRIMARY KEY,
    file_name    TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    posts        INTEGER NOT NULL DEFAULT 0,
    new_jobs     INTEGER NOT NULL DEFAULT 0
);
