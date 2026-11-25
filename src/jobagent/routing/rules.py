"""Stage 3a: free, deterministic rules that run before any AI call.

A rule either ends routing (SKIP, LINKEDIN_LEAD), caps the best route a job can reach (a
scam-flagged job can never be APPLY), or just records a fact. Rules only SKIP on clear
evidence from the employer's own text; anything subtle is left to the AI layer, which must
quote the posting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from jobagent.routing.geo import find_restrictions, read_places
from jobagent.routing.pay import PART_TIME, PayEstimate, estimate_monthly_usd

ROUTES = ("APPLY", "REVIEW", "CHECK_ELIGIBILITY", "SKIP", "LINKEDIN_LEAD")


@dataclass
class RuleHit:
    rule: str
    outcome: str  # SKIP | LEAD | CAP_REVIEW | CAP_CHECK | INFO
    detail: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "outcome": self.outcome, "detail": self.detail}


@dataclass
class RuleResult:
    final_route: str | None = None  # set when rules alone decide
    cap: str | None = None  # REVIEW or CHECK_ELIGIBILITY: best route the job may reach
    hits: list[RuleHit] = field(default_factory=list)
    quote: str | None = None
    eligibility: str | None = None
    pay: PayEstimate | None = None

    def add(self, rule: str, outcome: str, detail: str) -> None:
        self.hits.append(RuleHit(rule, outcome, detail))
        if outcome == "CAP_REVIEW":
            self.cap = "REVIEW"
        elif outcome == "CAP_CHECK" and self.cap != "REVIEW":
            self.cap = "CHECK_ELIGIBILITY"


def apply_cap(route: str, cap: str | None) -> str:
    """Lower a route to the cap. SKIP always stays SKIP."""
    if cap is None or route == "SKIP":
        return route
    if cap == "REVIEW":
        return "REVIEW"
    if cap == "CHECK_ELIGIBILITY" and route == "APPLY":
        return "CHECK_ELIGIBILITY"
    return route


def _rx(terms: list[str]) -> re.Pattern[str] | None:
    terms = [t for t in terms if t]
    return (
        re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I) if terms else None
    )


def is_part_time(job: dict) -> bool:
    desc = (job.get("description") or "")[:3000]
    blob = f"{job.get('employment_type') or ''} {job.get('title') or ''} {desc}"
    return bool(PART_TIME.search(blob))


def apply_rules(job: dict, profile, fx_to_usd: dict[str, float]) -> RuleResult:
    res = RuleResult()
    flags = job.get("flags") or []
    if isinstance(flags, str):
        flags = json.loads(flags)
    title = job.get("title") or ""
    employer_text = job.get("text_origin") == "employer"

    # 1. LinkedIn leads with no employer advert go straight to the digest for a manual check.
    if "linkedin_lead" in flags:
        res.add(
            "linkedin_lead", "LEAD", "LinkedIn alert lead; the employer's own advert was not found"
        )
        res.final_route = "LINKEDIN_LEAD"
        return res

    # 2. Function and seniority out of scope (title only: cheap and unambiguous).
    if (rx := _rx(profile.exclude_title_terms)) and (m := rx.search(title)):
        res.add("function", "SKIP", f'title "{title}" matches excluded function "{m.group(0)}"')
        res.final_route = "SKIP"
        return res
    if (rx := _rx(profile.seniority_exclude)) and (m := rx.search(title)):
        res.add("seniority", "SKIP", f'title "{title}" is outside mid/senior ("{m.group(0)}")')
        res.final_route = "SKIP"
        return res

    # 3. Location restrictions, from the employer's own text only.
    if employer_text:
        loc = job.get("location_text") or ""
        reading = read_places(loc)
        restrictions = find_restrictions(job.get("description") or "")
        inclusive_stmt = next((r for r in restrictions if r.reading.inclusive), None)
        excluding_stmt = next((r for r in restrictions if r.reading.excludes_kenya), None)
        if reading.inclusive or inclusive_stmt:
            res.quote = loc if reading.inclusive else inclusive_stmt.quote
            res.add(
                "location",
                "INFO",
                f'employer text includes Kenya, Africa or worldwide: "{res.quote}"',
            )
            # A contradiction is not proof: e.g. location field "Egypt, Argentina, Nigeria" but
            # the description says "open to candidates based in ... Africa".
            if not reading.inclusive and reading.excludes_kenya:
                res.add(
                    "location_conflict",
                    "CAP_CHECK",
                    f'location field "{loc}" does not list Kenya, but the description says '
                    f'"{inclusive_stmt.quote}"; confirm Kenya is accepted',
                )
        elif reading.excludes_kenya:
            res.quote, res.eligibility = loc, "EXCLUDED"
            res.add("location", "SKIP", f'location field lists only other places: "{loc}"')
            res.final_route = "SKIP"
            return res
        elif excluding_stmt:
            res.quote, res.eligibility = excluding_stmt.quote, "EXCLUDED"
            res.add("location", "SKIP", f'posting restricts applicants: "{excluding_stmt.quote}"')
            res.final_route = "SKIP"
            return res
    else:
        origin = job.get("text_origin")
        if job.get("source") == "reddit":
            res.add("text_origin", "CAP_REVIEW", "Reddit post: never APPLY")
        else:
            res.add(
                "text_origin",
                "CAP_CHECK",
                f"{origin} text, not the employer's own posting: eligibility cannot be confirmed",
            )

    # 4. Pay floor (only when pay is clearly stated).
    part_time = is_part_time(job)
    pay = estimate_monthly_usd(
        salary_min=job.get("salary_min"),
        salary_max=job.get("salary_max"),
        currency=job.get("salary_currency"),
        period=job.get("salary_period"),
        salary_text=job.get("salary_text"),
        description=job.get("description") or "",
        is_part_time=part_time,
        fx_to_usd=fx_to_usd,
        hours_part_time=profile.pay["hours_per_month_part_time"],
        hours_full_time=profile.pay["hours_per_month_full_time"],
    )
    if pay:
        res.pay = pay
        floor = profile.pay[
            "floor_usd_month_part_time" if part_time else "floor_usd_month_full_time"
        ]
        band = floor * (1 - profile.pay["review_band_pct"] / 100)
        kind = "part-time" if part_time else "full-time"
        msg = (
            f"about US${pay.monthly_usd_top:,.0f}/month at the top ({pay.basis}); "
            f"{kind} floor US${floor:,.0f}"
        )
        if pay.monthly_usd_top < band:
            res.add("pay", "SKIP", msg)
            res.final_route = "SKIP"
            return res
        if pay.monthly_usd_top < floor:
            res.add("pay", "CAP_REVIEW", msg + "; within 10% below the floor, so REVIEW")
        else:
            res.add("pay", "INFO", msg)

    # 5. Scam warnings cap at REVIEW, with the reasons.
    if "scam_risk" in flags:
        reasons = job.get("risk_reasons") or []
        if isinstance(reasons, str):
            reasons = json.loads(reasons)
        res.add("scam", "CAP_REVIEW", "; ".join(reasons) or "flagged as a possible scam")
    if "date_unknown" in flags:
        res.add("date", "INFO", "posting date unknown; age limit could not be checked")
    return res
