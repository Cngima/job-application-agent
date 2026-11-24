"""Trace a discovery job back to the employer's own posting.

Aggregators' location labels are not evidence. This resolver looks for the same role on the
company's public ATS board (Greenhouse, Lever, Ashby or Workable) using official APIs only:

1. If the job's apply link already is an ATS posting, fetch that posting.
2. Otherwise look the company up in the ats_lookup cache, or probe a few likely board slugs
   (capped per run, results cached for weeks), then match the title on that board.

On success the job keeps its discovery source and attribution, but its description and
location come from the employer and `text_origin` becomes "employer".
"""

from __future__ import annotations

import logging
import re
import sqlite3
from difflib import SequenceMatcher
from urllib.parse import quote

from jobagent.sourcing.ats import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    match_ats_url,
)
from jobagent.sourcing.http import HostUnavailable, PoliteClient, RobotsDisallowed
from jobagent.sourcing.models import Job
from jobagent.sourcing.store import get_ats_lookup, set_ats_lookup
from jobagent.sourcing.text import norm_company, norm_words

log = logging.getLogger(__name__)

TITLE_MATCH = 0.9
NAME_MATCH = 0.8


def titles_match(a: str, b: str) -> bool:
    a, b = norm_words(a), norm_words(b)
    return a == b or SequenceMatcher(None, a, b).ratio() >= TITLE_MATCH


def names_match(a: str, b: str) -> bool:
    """Board name vs company name. Plain containment is not enough: the Greenhouse board
    "General Assembly Remote Jobs" contains "Remote" but is not Remote.com."""
    a, b = norm_company(a), norm_company(b)
    if not a or not b:
        return False
    if a == b or SequenceMatcher(None, a, b).ratio() >= NAME_MATCH:
        return True
    short, long_ = sorted((a.split(), b.split()), key=len)
    return set(short) <= set(long_) and len(long_) - len(short) <= 1  # "Acme" vs "Acme Labs"


def slug_candidates(company: str, hint: str | None = None) -> list[str]:
    """Likely board slugs: the aggregator's own company slug, then the name with and without
    legal suffixes ('Acme Co.' -> 'acme', 'acmeco'), joined and hyphenated."""
    cands = [hint] if hint else []
    for words in (norm_company(company).split(), norm_words(company).split()):
        if words:
            cands += ["".join(words), "-".join(words)]
    return list(dict.fromkeys(c for c in cands if c))[:4]


class Resolver:
    def __init__(
        self, conn: sqlite3.Connection, client: PoliteClient, max_new_companies: int = 15
    ) -> None:
        self.conn = conn
        self.client = client
        self.max_new_companies = max_new_companies
        self.new_lookups = 0
        self.last_outcome = ""
        self.last_board: tuple[str, str] | None = None
        self._boards: dict[tuple[str, str], list[dict] | None] = {}

    # ---- board listings (cached for this run)

    def _board(self, ats: str, slug: str) -> list[dict] | None:
        key = (ats, slug)
        if key in self._boards:
            return self._boards[key]
        jobs: list[dict] | None = None
        try:
            if ats == "greenhouse":
                r = self.client.get(f"{GreenhouseSource.API.format(slug=slug)}/jobs")
                if r.status_code == 200:
                    jobs = [
                        {"title": j["title"], "ref": str(j["id"])} for j in r.json().get("jobs", [])
                    ]
            elif ats == "lever":
                r = self.client.get(LeverSource.API.format(slug=slug), params={"mode": "json"})
                if r.status_code == 200 and isinstance(r.json(), list):
                    jobs = [{"title": j["text"], "ref": j["id"]} for j in r.json()]
            elif ats == "ashby":
                r = self.client.get(AshbySource.API.format(slug=quote(slug)))
                if r.status_code == 200:
                    jobs = [{"title": j["title"], "ref": j["id"]} for j in r.json().get("jobs", [])]
            elif ats == "workable":
                r = self.client.get(WorkableSource.LIST.format(slug=slug))
                if r.status_code == 200:
                    jobs = [
                        {"title": j["title"], "ref": j["shortcode"]}
                        for j in r.json().get("jobs", [])
                    ]
        except (RobotsDisallowed, HostUnavailable, ValueError) as e:
            log.info("board %s:%s unreadable: %s", ats, slug, e)
        self._boards[key] = jobs or None
        return self._boards[key]

    def _board_name_ok(self, ats: str, slug: str, company: str) -> bool:
        """Greenhouse and Workable publish board names; check them to avoid slug collisions
        (the Greenhouse board 'remote' belongs to General Assembly, not Remote.com)."""
        try:
            if ats == "greenhouse":
                r = self.client.get(GreenhouseSource.API.format(slug=slug))
                return r.status_code == 200 and names_match(r.json().get("name", ""), company)
            if ats == "workable":
                r = self.client.get(WorkableSource.LIST.format(slug=slug))
                return r.status_code == 200 and names_match(r.json().get("name", ""), company)
        except (RobotsDisallowed, HostUnavailable, ValueError):
            return False
        return True  # Lever and Ashby expose no name; the title match must carry it

    def _find_board(self, company: str, title: str, hint: str | None) -> tuple[str, str] | None:
        key = norm_company(company)
        cached = get_ats_lookup(self.conn, key)
        if cached is None:
            self.last_outcome = "no_board"
            return None
        if cached:
            return cached
        if self.new_lookups >= self.max_new_companies:
            self.last_outcome = "capped"  # not looked up this run: try again next run
            return None
        self.new_lookups += 1
        for slug in slug_candidates(company, hint):
            for ats in ("greenhouse", "lever", "ashby", "workable"):
                board = self._board(ats, slug)
                if (
                    board
                    and any(titles_match(j["title"], title) for j in board)
                    and (self._board_name_ok(ats, slug, company))
                ):
                    set_ats_lookup(self.conn, key, ats, slug)
                    return ats, slug
        set_ats_lookup(self.conn, key, None, None)
        self.last_outcome = "no_board"
        return None

    # ---- public API

    def resolve(self, job: Job, slug_hint: str | None = None) -> Job | None:
        """Return the job enriched with the employer's posting, or None if not found.

        Afterwards `last_outcome` says why: found | no_board | no_match | capped | error, and
        `last_board` holds (ats, slug) when the company's board was identified.
        """
        employer: Job | None = None
        self.last_outcome, self.last_board = "no_match", None
        try:
            hit = match_ats_url(job.apply_url or "") or match_ats_url(job.url)
            if hit:
                cls, slug, ref = hit
                employer = cls.fetch_one(self.client, slug, ref, job.company, job.source_kind)
            else:
                board = self._find_board(job.company, job.title, slug_hint)
                if board:
                    self.last_board = board
                    ats, slug = board
                    match = next(
                        (
                            j
                            for j in self._board(ats, slug) or []
                            if titles_match(j["title"], job.title)
                        ),
                        None,
                    )
                    if match:
                        cls = {
                            "greenhouse": GreenhouseSource,
                            "lever": LeverSource,
                            "ashby": AshbySource,
                            "workable": WorkableSource,
                        }[ats]
                        employer = cls.fetch_one(
                            self.client, slug, match["ref"], job.company, job.source_kind
                        )
        except (RobotsDisallowed, HostUnavailable) as e:
            log.info("resolver blocked for %s: %s", job.title, e)
            self.last_outcome = "error"
            return None
        except Exception as e:  # a resolver failure must never fail the run
            log.warning("resolver error for %s @ %s: %s", job.title, job.company, e)
            self.last_outcome = "error"
            return None
        if not employer or not employer.description:
            return None
        self.last_outcome = "found"
        return job.model_copy(
            update={
                "employer_url": employer.employer_url,
                "apply_url": employer.apply_url or job.apply_url,
                "description": employer.description,
                "location_text": employer.location_text,
                "remote": employer.remote if employer.remote is not None else job.remote,
                "text_origin": "employer",
                "employment_type": employer.employment_type or job.employment_type,
            }
        )


def himalayas_slug_hint(job: Job) -> str | None:
    m = re.search(r"himalayas\.app/companies/([\w-]+)/", job.url)
    return m.group(1) if m else None
