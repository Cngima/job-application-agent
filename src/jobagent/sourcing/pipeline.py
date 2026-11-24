"""Run every enabled source: fetch, de-duplicate, resolve discovery jobs, store.

A failing source never fails the run: its error is recorded and the next source runs.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field

import httpx

from jobagent.db import now_iso
from jobagent.sourcing.ats import ATS_ADAPTERS, match_ats_url
from jobagent.sourcing.base import Source, SourceStats, SourcingContext
from jobagent.sourcing.config import SearchProfile, SourceConfig, SourcesFile
from jobagent.sourcing.discovery import DISCOVERY_ADAPTERS
from jobagent.sourcing.http import HostUnavailable, PoliteClient, RobotsDisallowed
from jobagent.sourcing.models import Job
from jobagent.sourcing.resolver import Resolver, himalayas_slug_hint
from jobagent.sourcing.store import insert_job, is_known, record_source_run
from jobagent.sourcing.text import canonical_url

log = logging.getLogger(__name__)

KIND_ORDER = {"employer_ats": 0, "agency": 1, "discovery": 2, "community": 3, "manual": 4}


@dataclass
class SourceReport:
    name: str
    status: str = "ok"  # ok | error | skipped | disabled
    listed: int = 0
    title_filtered: int = 0
    too_old: int = 0
    duplicates: int = 0
    new: int = 0
    resolved: int = 0
    requests: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)


def build_adapter(cfg: SourceConfig, client: PoliteClient, ctx: SourcingContext) -> Source:
    cls = ATS_ADAPTERS.get(cfg.adapter) or DISCOVERY_ADAPTERS.get(cfg.adapter)
    if cls is None:
        raise ValueError(f"no adapter for {cfg.adapter}")
    return cls(cfg, client, ctx)


def run_sourcing(
    conn: sqlite3.Connection,
    sources: SourcesFile,
    profile: SearchProfile,
    client: PoliteClient,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
    resolve: bool = True,
    max_detail_fetches: int = 60,
    max_new_companies: int = 15,
    max_age_days: int | None = 7,
) -> list[SourceReport]:
    resolver = Resolver(conn, client, max_new_companies=max_new_companies)
    seen_urls: set[str] = set()
    seen_keys: dict[str, str] = {}  # dedupe_key -> source that first produced it this run
    reports: list[SourceReport] = []

    gated: set[str] = set()  # posting URLs already passed through a detail gate this run

    def known_url(url: str) -> bool:
        cu = canonical_url(url)
        if cu in seen_urls or cu in gated:
            return True
        gated.add(cu)  # a board listing the same posting twice is fetched once
        return (
            conn.execute("SELECT 1 FROM jobs WHERE canonical_url = ?", (cu,)).fetchone() is not None
        )

    ctx = SourcingContext(
        title_ok=profile.title_prefilter.passes,
        max_detail_fetches=max_detail_fetches,
        known=known_url,
        max_age_days=max_age_days,
    )

    def cross_source_dupe(job: Job) -> bool:
        first = seen_keys.get(job.dedupe_key)
        return first is not None and first != job.source

    for cfg in sorted(sources.sources, key=lambda s: KIND_ORDER[s.kind]):
        if only and cfg.name not in only:
            continue
        rep = SourceReport(cfg.name)
        reports.append(rep)
        if not cfg.enabled:
            rep.status = "disabled"
            rep.notes.append(cfg.notes)
            continue
        started, before = now_iso(), client.requests_made
        adapter = build_adapter(cfg, client, ctx)
        try:
            for job in adapter.fetch():
                # Age limit for every source (adapters with dated lists check earlier, before
                # any detail request). Undated postings are kept but flagged.
                if not adapter.is_fresh(job.posted_at):
                    continue
                if job.posted_at is None and "date_unknown" not in job.flags:
                    job = job.model_copy(update={"flags": [*job.flags, "date_unknown"]})
                if job.canonical_url in seen_urls or cross_source_dupe(job) or is_known(conn, job):
                    rep.duplicates += 1
                    continue
                if resolve and job.source_kind == "discovery":
                    found = resolver.resolve(job, himalayas_slug_hint(job))
                    if found:
                        rep.resolved += 1
                        if found.canonical_url in seen_urls or is_known(conn, found):
                            rep.duplicates += 1
                            continue
                        job = found
                seen_urls.add(job.canonical_url)
                seen_keys.setdefault(job.dedupe_key, job.source)
                if not dry_run:
                    insert_job(conn, job)
                rep.new += 1
                if len(rep.sample) < 5:
                    rep.sample.append(f"{job.title} @ {job.company} [{job.text_origin}]")
        except RobotsDisallowed as e:
            rep.status, rep.error = "skipped", f"robots.txt disallows {e}"
        except HostUnavailable as e:
            rep.status, rep.error = "error", str(e)
        except httpx.HTTPStatusError as e:
            rep.status, rep.error = "error", f"HTTP {e.response.status_code} from {e.request.url}"
        except (httpx.TransportError, ValueError, KeyError) as e:
            rep.status, rep.error = "error", f"{type(e).__name__}: {e}"
        except Exception as e:  # never let one source break the run
            log.exception("source %s crashed", cfg.name)
            rep.status, rep.error = "error", f"{type(e).__name__}: {e}"
        st: SourceStats = adapter.stats
        rep.duplicates += st.known_skipped
        rep.too_old = st.too_old
        rep.listed, rep.title_filtered = st.listed, st.title_filtered
        rep.requests = client.requests_made - before
        rep.notes += st.errors
        if rep.status == "ok" and any(n.startswith("skipped") for n in st.errors):
            rep.status = "skipped"
        record_source_run(
            conn,
            cfg,
            started,
            st,
            rep.duplicates,
            rep.new,
            rep.resolved,
            rep.requests,
            rep.status,
            rep.error or "; ".join(st.errors) or None,
        )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    return reports


# --------------------------------------------------------------------------- manual intake


class ManualIntakeError(ValueError):
    pass


def add_manual(
    conn: sqlite3.Connection,
    client: PoliteClient,
    *,
    url: str | None = None,
    text: str | None = None,
    title: str | None = None,
    company: str | None = None,
    employer_text: bool = False,
) -> tuple[Job, bool]:
    """Add a job Carol found anywhere (X, WhatsApp, LinkedIn, hiring.cafe...).

    Supported ATS URLs are fetched through the polite client and count as employer text.
    Anything else is never fetched: the pasted text is used as is, and it only counts as
    employer text when Carol says it was copied from the employer's own posting.
    Returns (job, inserted).
    """
    job: Job | None = None
    if url and (hit := match_ats_url(url)):
        cls, slug, ref = hit
        job = cls.fetch_one(client, slug, ref, company or slug, "manual")
        if job is None:
            raise ManualIntakeError(f"could not read that {cls.ats} posting")
        job = job.model_copy(update={"source": "manual", "company": company or job.company})
    elif text:
        if not (title and company):
            raise ManualIntakeError("pasted text needs --title and --company")
        ref = url or f"manual://{hashlib.sha1(text.encode()).hexdigest()[:12]}"
        job = Job(
            source="manual",
            source_kind="manual",
            title=title,
            company=company,
            url=ref,
            employer_url=url if employer_text else None,
            description=text,
            text_origin="employer" if employer_text else "pasted",
        )
    elif url:
        raise ManualIntakeError(
            "That site is not a supported ATS, so the agent will not fetch it. "
            "Paste the posting text with --text-file (plus --title and --company)."
        )
    else:
        raise ManualIntakeError("give a URL, pasted text, or both")
    if is_known(conn, job):
        return job, False
    insert_job(conn, job)
    conn.commit()
    return job, True
