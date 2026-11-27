"""Labelled eval set: frozen postings plus the owner's confirmed labels.

Files (private set in data/private/evals/, public sample in data/sample/evals/):
- postings.jsonl  one frozen job per line (the fields the router reads), so the eval stays
                  reproducible after adverts close
- labels.yaml     per job id: route, eligibility verdict, a one-line reason, and whether the
                  owner has confirmed it
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from jobagent.sourcing.models import Job

FROZEN_FIELDS = [
    "id",
    "source",
    "source_kind",
    "text_origin",
    "title",
    "company",
    "url",
    "employer_url",
    "apply_url",
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
    "flags",
    "risk_reasons",
]

Route = Literal["APPLY", "REVIEW", "SKIP", "CHECK_ELIGIBILITY", "LINKEDIN_LEAD"]
Verdict = Literal["KENYA_CONFIRMED", "AFRICA_OR_WORLDWIDE_NO_EXCLUSION", "UNCLEAR", "EXCLUDED"]


class Label(BaseModel):
    route: Route
    eligibility: Verdict | None = None  # None where eligibility is not assessed (leads)
    reason: str
    confirmed: bool = False
    title: str = ""  # for reading the file; not used in scoring
    company: str = ""


def freeze_from_db(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in FROZEN_FIELDS}
        d["flags"] = json.loads(d["flags"] or "[]")
        d["risk_reasons"] = json.loads(d["risk_reasons"] or "[]")
        out.append(d)
    order = {i: n for n, i in enumerate(ids)}
    return sorted(out, key=lambda d: order[d["id"]])


def freeze_job(job: Job) -> dict:
    d = job.model_dump(mode="json")
    return {k: d.get(k) for k in FROZEN_FIELDS}


def write_postings(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for d in items:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")


def load_postings(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_labels(path: Path) -> dict[str, Label]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return {k: Label.model_validate(v) for k, v in raw.items()}


def write_labels(path: Path, labels: dict[str, Label], header: str = "") -> None:
    data = {k: v.model_dump() for k, v in labels.items()}
    with open(path, "w", encoding="utf-8") as fh:
        if header:
            fh.write(header)
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=100)
