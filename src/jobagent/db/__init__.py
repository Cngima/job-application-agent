"""SQLite connection and plain-SQL migrations.

Migrations are numbered files in db/migrations. `connect()` applies any that have not run
yet and records them in schema_version, so every command sees an up-to-date schema.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _migrations() -> list[tuple[int, str, str]]:
    files = resources.files("jobagent.db") / "migrations"
    out = []
    for f in files.iterdir():
        if f.name.endswith(".sql"):
            out.append((int(f.name.split("_", 1)[0]), f.name, f.read_text(encoding="utf-8")))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    applied = []
    for version, name, sql in _migrations():
        if version in done:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now_iso()),
            )
        applied.append(name)
        log.info("applied migration %s", name)
    return applied


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) a database and bring its schema up to date."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
