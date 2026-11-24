"""Read and write sourcing tables."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from jobagent.db import now_iso
from jobagent.sourcing.base import SourceStats
from jobagent.sourcing.config import SourceConfig
from jobagent.sourcing.models import Job

JOB_COLUMNS = [
    "id",
    "canonical_url",
    "dedupe_key",
    "source",
    "source_kind",
    "text_origin",
    "title",
    "company",
    "url",
    "employer_url",
    "apply_url",
    "external_id",
    "location_text",
    "aggregator_location",
    "description",
    "employment_type",
    "remote",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_text",
    "posted_at",
    "first_seen_at",
    "tags",
    "flags",
    "attribution",
]


def is_known(conn: sqlite3.Connection, job: Job) -> bool:
    """True if this posting is already stored.

    Same URL always counts. Same title and company counts only when it came from a different
    source: employers such as AJAIA post one title separately per country, and those postings
    must all be kept because only some of them may be open to Kenya.
    """
    # `url = ?` catches discovery leads stored under their employer URL after resolution, so
    # they are not traced to the employer again on every run.
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE canonical_url = ? OR url = ? "
        "OR (dedupe_key = ? AND source != ?) LIMIT 1",
        (job.canonical_url, job.url, job.dedupe_key, job.source),
    ).fetchone()
    return row is not None


def insert_job(conn: sqlite3.Connection, job: Job) -> None:
    d = job.model_dump(mode="json")
    d["first_seen_at"] = now_iso()
    d["tags"] = json.dumps(job.tags)
    d["flags"] = json.dumps(job.flags)
    d["remote"] = None if job.remote is None else int(job.remote)
    conn.execute(
        f"INSERT INTO jobs ({', '.join(JOB_COLUMNS)}) VALUES ({', '.join('?' * len(JOB_COLUMNS))})",
        [d.get(c) for c in JOB_COLUMNS],
    )


def upsert_source(conn: sqlite3.Connection, cfg: SourceConfig) -> None:
    conn.execute(
        "INSERT INTO sources (name, adapter, kind) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET adapter = excluded.adapter, kind = excluded.kind",
        (cfg.name, cfg.adapter, cfg.kind),
    )


def record_source_run(
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    started_at: str,
    stats: SourceStats,
    duplicates: int,
    new_jobs: int,
    resolved: int,
    requests: int,
    status: str,
    error: str | None,
) -> None:
    upsert_source(conn, cfg)
    conn.execute(
        "INSERT INTO sourcing_runs (started_at, finished_at, source, listed, title_filtered, "
        "too_old, duplicates, new_jobs, resolved, requests, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            started_at,
            now_iso(),
            cfg.name,
            stats.listed,
            stats.title_filtered,
            stats.too_old,
            duplicates,
            new_jobs,
            resolved,
            requests,
            status,
            error,
        ),
    )
    conn.execute(
        "UPDATE sources SET last_run_at = ?, last_status = ?, last_listed = ?, last_new = ?, "
        "last_error = ? WHERE name = ?",
        (now_iso(), status, stats.listed, new_jobs, error, cfg.name),
    )


# ---- ATS lookup cache

POSITIVE_TTL = timedelta(days=90)
NEGATIVE_TTL = timedelta(days=30)


def get_ats_lookup(conn: sqlite3.Connection, company_key: str) -> tuple[str, str] | None | bool:
    """(ats, slug) if known, None if known to have no public board, False if unknown/stale."""
    row = conn.execute(
        "SELECT ats, slug, checked_at FROM ats_lookup WHERE company_key = ?", (company_key,)
    ).fetchone()
    if not row:
        return False
    age = datetime.now(UTC) - datetime.fromisoformat(row["checked_at"])
    if row["ats"]:
        return (row["ats"], row["slug"]) if age < POSITIVE_TTL else False
    return None if age < NEGATIVE_TTL else False


def set_ats_lookup(
    conn: sqlite3.Connection, company_key: str, ats: str | None, slug: str | None
) -> None:
    conn.execute(
        "INSERT INTO ats_lookup (company_key, ats, slug, checked_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(company_key) DO UPDATE SET ats = excluded.ats, slug = excluded.slug, "
        "checked_at = excluded.checked_at",
        (company_key, ats, slug, now_iso()),
    )


def prune_old_jobs(conn: sqlite3.Connection, max_age_days: int, dry_run: bool = False) -> int:
    """Delete stored jobs posted more than `max_age_days` ago. Undated jobs are kept.

    Only the jobs table exists at this stage; once later stages add decisions or applications,
    jobs referenced there must never be pruned.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    where = "posted_at IS NOT NULL AND posted_at < ?"
    n = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", (cutoff,)).fetchone()[0]
    if not dry_run and n:
        conn.execute(f"DELETE FROM jobs WHERE {where}", (cutoff,))
        conn.commit()
    return n
