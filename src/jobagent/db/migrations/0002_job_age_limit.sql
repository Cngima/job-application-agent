-- Jobs older than the age limit (default 7 days) are dropped at sourcing; record how many.
ALTER TABLE sourcing_runs ADD COLUMN too_old INTEGER NOT NULL DEFAULT 0;
