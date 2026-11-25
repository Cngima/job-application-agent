"""Stage 3: route every undecided job to APPLY, REVIEW, SKIP, CHECK_ELIGIBILITY or LINKEDIN_LEAD.

Order per run:
1. Free rules for every job (some end here).
2. Discovery jobs that survived and only have aggregator text: trace to the employer's own
   posting (capped and cached), then re-run the rules on the employer's text.
3. AI in batches for the rest; policy checks and caps after the AI.
4. Every decision is stored with its rule hits, AI output, quote and reasons.
If the AI pauses (usage limit, call cap, provider down), remaining jobs stay undecided and are
picked up next run.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jobagent.ai.provider import AIError, AIRunner, InvalidOutput, ProviderPaused
from jobagent.db import now_iso
from jobagent.routing.ai_router import (
    BATCH_SCHEMA,
    PROMPT_VERSION,
    BatchVerdict,
    batch_prompt,
    enforce_policy,
    system_prompt,
)
from jobagent.routing.rules import RuleResult, apply_cap, apply_rules, is_part_time

log = logging.getLogger(__name__)


@dataclass
class RouteReport:
    considered: int = 0
    by_route: dict[str, int] = field(default_factory=dict)
    by_stage: dict[str, int] = field(default_factory=dict)
    resolved: int = 0
    pending: int = 0
    ai_calls: int = 0
    paused: str | None = None
    decisions: list[dict] = field(default_factory=list)

    def count(self, d: dict) -> None:
        self.by_route[d["route"]] = self.by_route.get(d["route"], 0) + 1
        self.by_stage[d["stage"]] = self.by_stage.get(d["stage"], 0) + 1
        self.decisions.append(d)


def _job_rows(
    conn: sqlite3.Connection,
    max_age_days: int | None,
    limit: int | None,
    redo: bool,
    ids: list[str] | None,
) -> list[dict]:
    where, args = [], []
    if ids:
        where.append(f"j.id IN ({','.join('?' * len(ids))})")
        args += ids
    elif not redo:
        where.append("NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id)")
    if max_age_days is not None and not ids:
        where.append("(j.posted_at IS NULL OR j.posted_at >= ?)")
        args.append((datetime.now(UTC) - timedelta(days=max_age_days)).isoformat())
    sql = "SELECT * FROM jobs j" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY j.first_seen_at DESC" + (f" LIMIT {int(limit)}" if limit else "")
    rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["flags"] = json.loads(r.get("flags") or "[]")
        r["risk_reasons"] = json.loads(r.get("risk_reasons") or "[]")
    return rows


def priority_score(job: dict, profile) -> int:
    """0 to 100: title close to a target title, priority keywords, part-time bonus."""
    title = (job.get("title") or "").lower()
    text = f"{title} {(job.get('description') or '')[:4000].lower()}"
    words = set(re.findall(r"[a-z]+", title))
    best = 0.0
    for t in profile.titles:
        tw = set(re.findall(r"[a-z]+", re.sub(r"\(.*?\)", "", t.lower())))
        if tw:
            best = max(best, len(words & tw) / len(tw))
    kw = sum(
        1 for k in profile.priority_keywords if re.search(rf"\b{re.escape(k.lower())}\b", text)
    )
    bonus = (profile.hours or {}).get("part_time_bonus", 5) if is_part_time(job) else 0
    return min(100, round(best * 50 + min(kw, 8) * 5 + bonus))


def _decision(
    job: dict,
    route: str,
    stage: str,
    rr: RuleResult,
    profile,
    *,
    verdict=None,
    quote=None,
    ai=None,
    ai_route=None,
    notes=(),
    provider=None,
    model=None,
) -> dict:
    reasons = list(ai.reasons) if ai else []
    reasons += list(notes)
    if not ai:
        reasons += [
            h.detail for h in rr.hits if h.outcome in ("SKIP", "LEAD", "CAP_REVIEW", "CAP_CHECK")
        ]
    return {
        "job_id": job["id"],
        "title": job.get("title"),
        "company": job.get("company"),
        "route": route,
        "stage": stage,
        "eligibility_verdict": verdict or rr.eligibility,
        "quoted_location_text": quote if quote is not None else rr.quote,
        "function_match": ai.function_match if ai else None,
        "seniority_match": ai.seniority_match if ai else None,
        "workplace": ai.workplace if ai else None,
        "hours_requirement": ai.hours_requirement.model_dump() if ai else None,
        "pay_estimate": vars(rr.pay) if rr.pay else None,
        "priority": priority_score(job, profile) if route != "SKIP" else 0,
        "rule_hits": [h.as_dict() for h in rr.hits],
        "reasons": reasons,
        "ai_route": ai_route,
        "provider": provider,
        "model": model,
        "prompt_version": PROMPT_VERSION if ai else None,
    }


def store_decision(conn: sqlite3.Connection, d: dict) -> None:
    conn.execute(
        "INSERT INTO decisions (job_id, decided_at, route, stage, eligibility_verdict, "
        "quoted_location_text, function_match, seniority_match, workplace, hours_requirement, "
        "pay_estimate, priority, rule_hits, reasons, ai_route, provider, model, prompt_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            d["job_id"],
            now_iso(),
            d["route"],
            d["stage"],
            d["eligibility_verdict"],
            d["quoted_location_text"],
            d["function_match"],
            d["seniority_match"],
            d["workplace"],
            json.dumps(d["hours_requirement"]) if d["hours_requirement"] else None,
            json.dumps(d["pay_estimate"]) if d["pay_estimate"] else None,
            d["priority"],
            json.dumps(d["rule_hits"]),
            json.dumps(d["reasons"]),
            d["ai_route"],
            d["provider"],
            d["model"],
            d["prompt_version"],
        ),
    )


def _update_job_from_employer(conn: sqlite3.Connection, job: dict, found) -> dict:
    conn.execute(
        "UPDATE jobs SET employer_url = ?, apply_url = ?, description = ?, location_text = ?, "
        "remote = ?, employment_type = ?, text_origin = 'employer' WHERE id = ?",
        (
            found.employer_url,
            found.apply_url,
            found.description,
            found.location_text,
            None if found.remote is None else int(found.remote),
            found.employment_type,
            job["id"],
        ),
    )
    job = dict(job)
    job.update(
        employer_url=found.employer_url,
        apply_url=found.apply_url,
        description=found.description,
        location_text=found.location_text,
        remote=found.remote,
        employment_type=found.employment_type,
        text_origin="employer",
    )
    return job


def route_jobs(
    conn: sqlite3.Connection,
    profile,
    routing_cfg,
    runner: AIRunner | None,
    *,
    resolver=None,
    batch_size: int = 5,
    limit: int | None = None,
    redo: bool = False,
    dry_run: bool = False,
    job_ids: list[str] | None = None,
) -> RouteReport:
    rep = RouteReport()
    fx = routing_cfg.fx_to_usd
    jobs = _job_rows(conn, profile.max_age_days, limit, redo, job_ids)
    rep.considered = len(jobs)

    survivors: list[tuple[dict, RuleResult]] = []
    for job in jobs:
        rr = apply_rules(job, profile, fx)
        if (
            rr.final_route is None
            and resolver is not None
            and job.get("source_kind") == "discovery"
            and job.get("text_origin") != "employer"
        ):
            from jobagent.sourcing.models import Job

            lead = Job(
                source=job["source"],
                source_kind="discovery",
                title=job["title"],
                company=job["company"],
                url=job["url"],
                apply_url=job.get("apply_url"),
                text_origin=job["text_origin"],
            )
            found = resolver.resolve(lead)
            if found:
                rep.resolved += 1
                job = (
                    _update_job_from_employer(conn, job, found)
                    if not dry_run
                    else {
                        **job,
                        "description": found.description,
                        "location_text": found.location_text,
                        "text_origin": "employer",
                    }
                )
                rr = apply_rules(job, profile, fx)
        if rr.final_route:
            stage = "lead" if rr.final_route == "LINKEDIN_LEAD" else "rules"
            rep.count(_decision(job, rr.final_route, stage, rr, profile))
        else:
            survivors.append((job, rr))

    if survivors and runner is None:
        rep.pending = len(survivors)  # rules-only run: AI decides these later
    elif survivors:
        system = system_prompt(profile)
        for i in range(0, len(survivors), batch_size):
            batch = survivors[i : i + batch_size]
            jobs_only = [j for j, _ in batch]
            try:
                parsed, res = runner.call(
                    "route",
                    system,
                    batch_prompt(jobs_only, routing_cfg.max_desc_chars),
                    BATCH_SCHEMA,
                    BatchVerdict,
                    items=len(batch),
                )
            except ProviderPaused as e:
                rep.paused = str(e)
                rep.pending += len(survivors) - i
                break
            except (InvalidOutput, AIError) as e:
                for job, rr in batch:
                    d = _decision(
                        job,
                        apply_cap("REVIEW", rr.cap),
                        "ai_failed",
                        rr,
                        profile,
                        notes=[f"AI routing failed ({str(e)[:120]}); needs a human look"],
                    )
                    rep.count(d)
                continue
            by_id = {v.job_id: v for v in parsed.jobs}
            for job, rr in batch:
                v = by_id.get(job["id"])
                if v is None:
                    rep.count(
                        _decision(
                            job,
                            apply_cap("REVIEW", rr.cap),
                            "ai_failed",
                            rr,
                            profile,
                            notes=["AI returned no answer for this job"],
                        )
                    )
                    continue
                route, verdict, notes = enforce_policy(v, job, profile)
                capped = apply_cap(route, rr.cap)
                if capped != route:
                    cap_hit = next(h for h in rr.hits if h.outcome.startswith("CAP"))
                    notes.append(f"capped at {capped}: {cap_hit.detail}")
                rep.count(
                    _decision(
                        job,
                        capped,
                        "ai",
                        rr,
                        profile,
                        verdict=verdict,
                        quote=v.quoted_location_text,
                        ai=v,
                        ai_route=v.route,
                        notes=notes,
                        provider=res.provider,
                        model=res.model,
                    )
                )
        rep.ai_calls = runner.calls

    if not dry_run:
        for d in rep.decisions:
            store_decision(conn, d)
        conn.commit()
    return rep
