"""Scam checks for WhatsApp, manual and community postings.

Each check returns a plain-English reason. A flagged job goes to REVIEW with its reasons and
can never be routed to APPLY (enforced in Stage 3). These are heuristics: they flag for a human
to look, they do not decide.
"""

from __future__ import annotations

import re

FREE_EMAIL_DOMAINS = re.compile(
    r"@(gmail|googlemail|yahoo|ymail|hotmail|outlook|live|aol|icloud|proton|protonmail|gmx|mail)"
    r"\.[a-z.]+\b",
    re.I,
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+", re.I)
# Kenyan mobiles (07xx, 01xx, +254) and other international numbers.
PHONE = re.compile(
    r"(?:\+?254|\b0)[17]\d{2}[\s-]?\d{3}[\s-]?\d{3}\b|\+\d{1,3}[\s-]?\d{3,4}[\s-]?\d{3}[\s-]?\d{3,4}"
)

FEE = re.compile(
    r"\b(registration|application|processing|training|onboarding|interview|placement|"
    r"admin(?:istration)?|medical|clearance|joining|consultation)\s+fees?\b"
    r"|\bpay\s+(?:a\s+)?(?:small\s+|one[- ]off\s+|refundable\s+)?fee\b"
    r"|\bfees?\s+of\s+(?:ksh|kes|\$|usd)"
    r"|\b(?:pay|send|deposit|payment|transfer)\W+(?:\w+\W+){0,4}m-?pesa\b"
    r"|\bm-?pesa\W+(?:\w+\W+){0,3}(?:payment|number|till|paybill)\b"
    r"|\bpaybill\b|\btill\s+(?:number|no)\b"
    r"|\bsend\s+(?:ksh|kes|money|cash)\b|\bpay\s+(?:ksh|kes)\b",
    re.I,
)
ID_UPFRONT = re.compile(
    r"\b(?:send|submit|share|attach|provide|upload|forward)\W+(?:\w+\W+){0,4}"
    r"(?:national\s+id|id\s+(?:card|copy|number)|copy\s+of\s+(?:your\s+)?id|\bid\b|passport|"
    r"kra\s+pin|bank\s+(?:details|statement|account)|birth\s+certificate|certificate\s+of\s+good\s+conduct)",
    re.I,
)
HYPE = re.compile(
    r"\bearn\s+up\s+to\b|\bguaranteed\s+(?:income|earnings|pay|salary)\b|\bdaily\s+pay(?:out)?\b"
    r"|\bget\s+paid\s+daily\b|\bwork\s+from\s+your\s+phone\b|\bno\s+(?:experience|skills)\s+"
    r"(?:needed|required)\b.{0,80}(?:\$|ksh|kes|usd)",
    re.I | re.S,
)
# Amount per period, e.g. "$120/hour", "KES 25,000 per day".
PAY = re.compile(
    r"(?P<cur>\$|usd|ksh|kes)\s?\.?\s?(?P<amt>\d[\d,]*(?:\.\d+)?)\s?(?P<k>k)?\s*"
    r"(?:per|/|a|an|each)\s*(?P<per>hour|hr|day|week)",
    re.I,
)
UNREALISTIC = {  # anything above these is "too good to be true" for remote coordination work
    ("usd", "hour"): 100,
    ("usd", "day"): 500,
    ("usd", "week"): 3000,
    ("kes", "hour"): 10_000,
    ("kes", "day"): 40_000,
    ("kes", "week"): 200_000,
}


def _unrealistic_pay(text: str) -> str | None:
    for m in PAY.finditer(text):
        cur = "usd" if m.group("cur").lower() in ("$", "usd") else "kes"
        per = "hour" if m.group("per").lower() in ("hour", "hr") else m.group("per").lower()
        amt = float(m.group("amt").replace(",", "")) * (1000 if m.group("k") else 1)
        if amt > UNREALISTIC[(cur, per)]:
            return f"promises unrealistic pay ({m.group(0).strip()})"
    if HYPE.search(text):
        return f'promises unrealistic pay ("{HYPE.search(text).group(0).strip()[:60]}")'
    return None


def scam_reasons(text: str, company: str | None = None) -> list[str]:
    """Return every scam warning found in a posting's own text (empty list = none)."""
    reasons: list[str] = []
    if m := FEE.search(text):
        reasons.append(f'asks for a fee or M-Pesa payment ("{m.group(0).strip()}")')

    emails = EMAIL.findall(text)
    urls = [u for u in URL.findall(text) if not re.search(r"wa\.me|whatsapp\.com", u, re.I)]
    phones = PHONE.findall(text)
    company_email = any(not FREE_EMAIL_DOMAINS.search(e) for e in emails)
    if (phones or re.search(r"wa\.me/", text, re.I)) and not urls and not company_email:
        reasons.append("gives only a personal phone or WhatsApp number to apply")

    named = company and not company.lower().startswith("unknown")
    free = [e for e in emails if FREE_EMAIL_DOMAINS.search(e)]
    if named and free:
        reasons.append(f"uses a free email address ({free[0]}) for a named company ({company})")

    if pay := _unrealistic_pay(text):
        reasons.append(pay)
    if m := ID_UPFRONT.search(text):
        reasons.append(f'asks for ID or bank documents up front ("{m.group(0).strip()[:60]}")')
    return reasons
