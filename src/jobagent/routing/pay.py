"""Turn stated pay into an estimated US$ per month, using the posting's own hours if given.

Only clearly stated figures are used: structured salary fields, a salary text field, or an
amount in the description that carries both a currency and a period ("$25/hour",
"KES 150,000 per month"). Anything else is "pay not stated", which never causes a SKIP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WEEKS_PER_MONTH = 4.33
DAYS_PER_MONTH = 21.67

CURRENCY_ALIASES = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "kes": "KES",
    "ksh": "KES",
    "kshs": "KES",
    "cad": "CAD",
    "c$": "CAD",
    "aud": "AUD",
    "a$": "AUD",
    "inr": "INR",
    "₹": "INR",
    "zar": "ZAR",
    "php": "PHP",
    "₱": "PHP",
    "ngn": "NGN",
    "₦": "NGN",
}
PERIOD_ALIASES = {
    "hour": "hour",
    "hr": "hour",
    "hourly": "hour",
    "h": "hour",
    "day": "day",
    "daily": "day",
    "week": "week",
    "wk": "week",
    "weekly": "week",
    "month": "month",
    "mo": "month",
    "monthly": "month",
    "pm": "month",
    "p.m.": "month",
    "year": "year",
    "yr": "year",
    "annum": "year",
    "annual": "year",
    "annually": "year",
    "pa": "year",
    "yearly": "year",
}
AMOUNT = re.compile(
    r"(?P<cur>US\$|C\$|A\$|\$|€|£|₹|₱|₦|\b(?:USD|EUR|GBP|KES|KSh|Kshs|CAD|AUD|INR|ZAR|PHP|NGN)\b)\s?"
    r"(?P<lo>\d[\d,]*(?:\.\d+)?)\s?(?P<lok>[kK])?"
    r"(?:\s?(?:-|–|to)\s?(?:US\$|C\$|A\$|\$|€|£|₹|₱|₦|(?:USD|EUR|GBP|KES|KSh|CAD|AUD|INR|ZAR|PHP|NGN))?\s?"
    r"(?P<hi>\d[\d,]*(?:\.\d+)?)\s?(?P<hik>[kK])?)?"
    r"\s*(?:/|per|an|a|each)\s*(?P<per>hour|hr|day|week|wk|month|mo|year|yr|annum)\b",
    re.I,
)
HOURS = re.compile(
    r"\b(?P<lo>\d{1,2})(?:\s?(?:-|–|to)\s?(?P<hi>\d{1,2}))?\s*(?:hours?|hrs?)\s*(?:per|a|/|each)\s*week\b",
    re.I,
)
PART_TIME = re.compile(r"\bpart[- ]?time\b", re.I)


@dataclass
class PayEstimate:
    monthly_usd_top: float
    monthly_usd_bottom: float
    basis: str  # human-readable: what was read and how it was converted
    hours_source: str | None = None  # "stated" | "assumed part-time" | "assumed full-time"


PAY_WORDS = re.compile(
    r"\b(salary|salaries|compensation|pay|paid|pay range|rate|base|wage|ote|earn|earning|"
    r"remuneration|income|package|budgeted|range for this role)\b",
    re.I,
)
# Sentences about perks, not pay: "learning budget of USD 2,000 per year", "$150 per month
# home-office stipend". Found on real postings; these must never trigger the pay floor.
BENEFIT_WORDS = re.compile(
    r"\b(budget|stipend|allowance|reimburs\w*|learning|education|training|wellness|well-being|"
    r"home[- ]office|equipment|internet|phone|mobile|coworking|co-working|gym|fitness|travel|"
    r"insurance|medical|health|pension|401k|bonus|referral|perk|benefit|credit|subscription)\b",
    re.I,
)


def pay_in_description(text: str) -> re.Match[str] | None:
    """First amount in a sentence that is about pay and not about a perk."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if BENEFIT_WORDS.search(sentence) or not PAY_WORDS.search(sentence):
            continue
        if m := AMOUNT.search(sentence):
            return m
    return None


def _num(s: str | None, k: str | None) -> float | None:
    if not s:
        return None
    v = float(s.replace(",", ""))
    return v * 1000 if k else v


def stated_hours_per_month(text: str) -> float | None:
    m = HOURS.search(text or "")
    if not m:
        return None
    hi = int(m.group("hi") or m.group("lo"))
    return hi * WEEKS_PER_MONTH if 1 <= hi <= 60 else None


def _monthly(amount: float, period: str, hours_month: float) -> float:
    return {
        "hour": amount * hours_month,
        "day": amount * DAYS_PER_MONTH,
        "week": amount * WEEKS_PER_MONTH,
        "month": amount,
        "year": amount / 12,
    }[period]


def estimate_monthly_usd(
    *,
    salary_min: float | None,
    salary_max: float | None,
    currency: str | None,
    period: str | None,
    salary_text: str | None,
    description: str,
    is_part_time: bool,
    fx_to_usd: dict[str, float],
    hours_part_time: float = 80,
    hours_full_time: float = 160,
) -> PayEstimate | None:
    """Best reading of stated pay, or None when pay is not clearly stated."""
    stated = stated_hours_per_month(description) or stated_hours_per_month(salary_text or "")
    hours = stated or (hours_part_time if is_part_time else hours_full_time)
    hours_src = (
        "stated" if stated else ("assumed part-time" if is_part_time else "assumed full-time")
    )

    lo = hi = None
    cur = per = None
    source = ""
    if (salary_min or salary_max) and currency and period:
        lo, hi, cur, per = (
            salary_min or salary_max,
            salary_max or salary_min,
            currency.upper(),
            period,
        )
        source = "salary fields"
    else:
        for text, label in ((salary_text or "", "salary text"), (description, "description")):
            m = AMOUNT.search(text) if label == "salary text" else pay_in_description(text)
            if m:
                cur = CURRENCY_ALIASES.get(m.group("cur").lower(), m.group("cur").upper())
                lo = _num(m.group("lo"), m.group("lok"))
                hi = _num(m.group("hi"), m.group("hik") or m.group("lok")) or lo
                per = PERIOD_ALIASES.get(m.group("per").lower())
                source = f'{label}: "{m.group(0).strip()}"'
                break
    if lo is None or not per or per not in ("hour", "day", "week", "month", "year"):
        return None
    rate = fx_to_usd.get(cur or "")
    if rate is None:
        return None
    top = _monthly(max(lo, hi or lo) * rate, per, hours)
    bottom = _monthly(min(lo, hi or lo) * rate, per, hours)
    basis = f"{source}; {cur} per {per}"
    if per == "hour":
        basis += f" x {hours:.0f} h/month ({hours_src})"
    return PayEstimate(round(top, 2), round(bottom, 2), basis, hours_src if per == "hour" else None)
