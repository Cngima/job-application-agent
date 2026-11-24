"""Process WhatsApp text (inbox files or `jobagent add --batch`) and LinkedIn alert leads."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobagent.db import now_iso
from jobagent.intake.linkedin import LinkedInLead
from jobagent.intake.pages import fetch_advert, never_fetch
from jobagent.intake.scam import scam_reasons
from jobagent.intake.whatsapp import WhatsAppPost, split_posts
from jobagent.sourcing.config import load_sources
from jobagent.sourcing.http import PoliteClient
from jobagent.sourcing.models import Job
from jobagent.sourcing.resolver import Resolver
from jobagent.sourcing.store import insert_job, is_known

log = logging.getLogger(__name__)

WHATSAPP = "whatsapp"
ORIGINAL_POST = "\n\n--- Original WhatsApp post ---\n"
LINKEDIN = "linkedin_alert"


@dataclass
class IntakeReport:
    name: str
    posts: int = 0
    new: int = 0
    duplicates: int = 0
    too_old: int = 0
    title_filtered: int = 0
    links_followed: int = 0
    employer_found: int = 0
    scam_flagged: int = 0
    linkedin_leads: int = 0
    boards_added: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)


def _fresh(posted_at: datetime | None, max_age_days: int | None) -> bool:
    if posted_at is None or max_age_days is None:
        return True
    return datetime.now(UTC) - posted_at <= timedelta(days=max_age_days)


def _with_scam_checks(job: Job, post_text: str) -> Job:
    reasons = scam_reasons(post_text, job.company)
    if not reasons:
        return job
    return job.model_copy(
        update={
            "flags": [*job.flags, "scam_risk"] if "scam_risk" not in job.flags else job.flags,
            "risk_reasons": reasons,
        }
    )


# --------------------------------------------------------------------------- WhatsApp


def post_to_job(
    post: WhatsAppPost,
    client: PoliteClient,
    resolver: Resolver | None,
    rep: IntakeReport,
    max_links: int = 2,
    source: str = WHATSAPP,
) -> Job:
    """Build a Job from a post, following its apply links to the original advert if allowed."""
    url = next((u for u in post.links if not never_fetch(u)), None) or (
        post.links[0] if post.links else f"whatsapp://{post.content_hash}"
    )
    job = Job(
        source=source,
        source_kind="community",
        title=post.title or "Untitled post",
        company=post.company or "Unknown employer",
        url=url,
        description=post.text,
        text_origin="community",
        aggregator_location=post.location,
        posted_at=post.posted_at,
        apply_url=url if url.startswith("http") else None,
    )
    for link in [u for u in post.links if not never_fetch(u)][:max_links]:
        rep.links_followed += 1
        advert = fetch_advert(client, link, kind="community")
        # Employer text is kept however short; other pages must say something substantial.
        if (
            not advert
            or not advert.description.strip()
            or (not advert.is_employer and len(advert.description) < 80)
        ):
            continue
        update = {
            "url": advert.url,
            "apply_url": advert.url,
            "title": advert.title
            if post.title in (None, "Untitled post") and advert.title
            else job.title,
            "company": job.company if post.company else (advert.company or job.company),
            "description": f"{advert.description}{ORIGINAL_POST}{post.text}",
            "posted_at": advert.posted_at or job.posted_at,
        }
        if advert.is_employer:
            update |= {
                "employer_url": advert.url,
                "text_origin": "employer",
                "location_text": advert.location,
            }
            rep.employer_found += 1
        else:
            update |= {"text_origin": "aggregator"}
        job = job.model_copy(update=update)
        break
    # No readable advert: if the company is named, look for the role on its own ATS board.
    if job.text_origin != "employer" and resolver and post.company:
        found = resolver.resolve(job)
        if found:
            rep.employer_found += 1
            job = found.model_copy(
                update={
                    "description": f"{found.description}{ORIGINAL_POST}{post.text}",
                }
            )
    return _with_scam_checks(job, post.text)


def process_whatsapp_text(
    conn: sqlite3.Connection,
    client: PoliteClient,
    text: str,
    *,
    name: str = WHATSAPP,
    resolver: Resolver | None = None,
    title_ok: Callable[[str], bool] | None = None,
    max_age_days: int | None = 7,
    max_links: int = 2,
    dry_run: bool = False,
) -> IntakeReport:
    rep = IntakeReport(name)
    for post in split_posts(text):
        rep.posts += 1
        if not _fresh(post.posted_at, max_age_days):
            rep.too_old += 1
            continue
        probe = Job(
            source=WHATSAPP,
            source_kind="community",
            title=post.title or "x",
            company=post.company or "Unknown employer",
            url=post.links[0] if post.links else f"whatsapp://{post.content_hash}",
        )
        if is_known(conn, probe):
            rep.duplicates += 1
            continue
        if title_ok and post.title and not title_ok(post.title) and not title_ok(post.text[:300]):
            rep.title_filtered += 1
            continue
        job = post_to_job(post, client, resolver, rep, max_links)
        if not _fresh(job.posted_at, max_age_days):
            rep.too_old += 1
            continue
        if job.posted_at is None:
            job = job.model_copy(update={"flags": [*job.flags, "date_unknown"]})
        if is_known(conn, job):
            rep.duplicates += 1
            continue
        if job.risk_reasons:
            rep.scam_flagged += 1
        if not dry_run:
            insert_job(conn, job)
        rep.new += 1
        if len(rep.sample) < 8:
            flag = " [SCAM RISK]" if job.risk_reasons else ""
            rep.sample.append(f"{job.title} @ {job.company} [{job.text_origin}]{flag}")
    if not dry_run:
        conn.commit()
    return rep


def process_inbox(
    conn: sqlite3.Connection,
    client: PoliteClient,
    inbox: Path,
    *,
    dry_run: bool = False,
    **kw,
) -> list[IntakeReport]:
    """Process every new .txt file in the inbox; move each to inbox/processed/ when done."""
    inbox.mkdir(parents=True, exist_ok=True)
    done_dir = inbox / "processed"
    reports = []
    for path in sorted(inbox.glob("*.txt")):
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        digest = hashlib.sha1(raw).hexdigest()[:16]
        seen = conn.execute(
            "SELECT 1 FROM inbox_files WHERE content_hash = ?", (digest,)
        ).fetchone()
        if seen:
            rep = IntakeReport(path.name, errors=["already processed (same content)"])
        else:
            try:
                rep = process_whatsapp_text(
                    conn, client, text, name=path.name, dry_run=dry_run, **kw
                )
            except Exception as e:  # leave the file in the inbox so nothing is lost
                log.exception("inbox file %s failed", path.name)
                reports.append(IntakeReport(path.name, errors=[f"{type(e).__name__}: {e}"]))
                continue
            if not dry_run:
                conn.execute(
                    "INSERT INTO inbox_files "
                    "(content_hash, file_name, processed_at, posts, new_jobs) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (digest, path.name, now_iso(), rep.posts, rep.new),
                )
                conn.commit()
        if not dry_run:
            done_dir.mkdir(exist_ok=True)
            target = done_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{path.name}"
            shutil.move(str(path), target)
        reports.append(rep)
    return reports


# --------------------------------------------------------------------------- LinkedIn leads


def add_board_to_sources(path: Path, company: str, ats: str, slug: str) -> bool:
    """Append a discovered board to config/sources.yaml (text insert keeps all comments).

    Returns False if that board is already configured. The file is validated after the edit
    and restored if it no longer loads.
    """
    current = load_sources(path)
    if any(s.adapter == ats and (s.slug or "").lower() == slug.lower() for s in current.sources):
        return False
    names = {s.name for s in current.sources}
    name = company if company not in names else f"{company} ({ats})"
    original = path.read_text(encoding="utf-8")
    marker = "  # ---------------- added automatically (found from LinkedIn job alerts)\n"
    entry = (
        f"  - {{name: {json.dumps(name)}, adapter: {ats}, slug: {json.dumps(slug)}, "
        f'kind: employer_ats, notes: "Added {datetime.now():%Y-%m-%d} from a LinkedIn alert '
        f'lead."}}\n'
    )
    if marker in original:
        i = original.index(marker) + len(marker)
        new = original[:i] + entry + original[i:]
    else:
        anchor = re.search(
            r"^(# Companies and agencies from the Job Hunt Log|manual_watch:)", original, re.M
        )
        if not anchor:
            raise ValueError("could not find where to insert in sources.yaml")
        new = original[: anchor.start()] + marker + entry + "\n" + original[anchor.start() :]
    path.write_text(new, encoding="utf-8")
    try:
        load_sources(path)
    except Exception:
        path.write_text(original, encoding="utf-8")
        raise
    return True


def lead_to_job(lead: LinkedInLead) -> Job:
    return Job(
        source=LINKEDIN,
        source_kind="discovery",
        title=lead.title,
        company=lead.company,
        url=lead.url,
        text_origin="aggregator",
        aggregator_location=lead.location,
        description=(
            f"LinkedIn job alert lead: {lead.title} at {lead.company}"
            + (f", {lead.location}" if lead.location else "")
            + ". The full advert is on LinkedIn, which the agent never opens."
        ),
        posted_at=lead.received_at,
        flags=["linkedin_lead"],
        attribution=f"LinkedIn job alert email ({lead.url})",
    )


def process_linkedin_leads(
    conn: sqlite3.Connection,
    resolver: Resolver,
    leads: list[LinkedInLead],
    *,
    sources_path: Path | None,
    title_ok: Callable[[str], bool] | None = None,
    max_age_days: int | None = 7,
    dry_run: bool = False,
    rep: IntakeReport | None = None,
) -> IntakeReport:
    """For each lead: find the same role on the company's own board, else keep a LINKEDIN_LEAD."""
    rep = rep or IntakeReport(LINKEDIN)
    for lead in leads:
        rep.posts += 1
        if not _fresh(lead.received_at, max_age_days):
            rep.too_old += 1
            continue
        job = lead_to_job(lead)
        if is_known(conn, job):
            rep.duplicates += 1
            continue
        if title_ok and not title_ok(lead.title):
            rep.title_filtered += 1
            continue
        found = resolver.resolve(job)
        if found:
            rep.employer_found += 1
            job = found.model_copy(
                update={"flags": [f for f in found.flags if f != "linkedin_lead"]}
            )
            if is_known(conn, job):
                rep.duplicates += 1
                continue
            if sources_path and resolver.last_board and not dry_run:
                ats, slug = resolver.last_board
                if add_board_to_sources(sources_path, lead.company, ats, slug):
                    rep.boards_added.append(f"{lead.company} ({ats}:{slug})")
        else:
            rep.linkedin_leads += 1
            if resolver.last_outcome == "capped":
                job = job.model_copy(update={"flags": [*job.flags, "lookup_pending"]})
        if not dry_run:
            insert_job(conn, job)
        rep.new += 1
        if len(rep.sample) < 8:
            tag = "employer board" if found else "LINKEDIN_LEAD"
            rep.sample.append(f"{job.title} @ {job.company} [{tag}]")
    if not dry_run:
        conn.commit()
    return rep


def retry_pending_leads(
    conn: sqlite3.Connection, resolver: Resolver, sources_path: Path | None, rep: IntakeReport
) -> None:
    """Leads skipped earlier because the lookup cap was reached get another try."""
    rows = conn.execute(
        "SELECT id, title, company, url, aggregator_location, posted_at FROM jobs "
        "WHERE source = ? AND flags LIKE '%lookup_pending%'",
        (LINKEDIN,),
    ).fetchall()
    for r in rows:
        job = Job(
            source=LINKEDIN,
            source_kind="discovery",
            title=r["title"],
            company=r["company"],
            url=r["url"],
            text_origin="aggregator",
            aggregator_location=r["aggregator_location"],
        )
        found = resolver.resolve(job)
        if resolver.last_outcome == "capped":
            break
        if found:
            conn.execute(
                "UPDATE jobs SET employer_url = ?, apply_url = ?, description = ?, "
                "location_text = ?, "
                "text_origin = 'employer', flags = '[]' WHERE id = ?",
                (
                    found.employer_url,
                    found.apply_url,
                    found.description,
                    found.location_text,
                    r["id"],
                ),
            )
            rep.employer_found += 1
            if sources_path and resolver.last_board:
                ats, slug = resolver.last_board
                if add_board_to_sources(sources_path, r["company"], ats, slug):
                    rep.boards_added.append(f"{r['company']} ({ats}:{slug})")
        else:
            conn.execute(
                "UPDATE jobs SET flags = ? WHERE id = ?", (json.dumps(["linkedin_lead"]), r["id"])
            )
    conn.commit()
