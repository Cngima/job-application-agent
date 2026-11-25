"""A deterministic stand-in for the AI router, used by the `fake` provider in CI and the demo.

It is deliberately simple keyword logic, not a model. Evals report it as a baseline so the
real providers can be compared against it.
"""

from __future__ import annotations

import json
import re

KENYA = re.compile(r"[^.\n;]*\b(kenya|nairobi|nakuru)\b[^.\n;]*", re.I)
WIDE = re.compile(
    r"[^.\n;]*\b(africa|worldwide|anywhere in the world|globally remote|work from anywhere)\b"
    r"[^.\n;]*",
    re.I,
)
EXCL = re.compile(
    r"[^.\n;]*\b(us only|u\.s\. only|"
    r"must (?:be )?(?:based|reside|live) in the (?:us|united states)|"
    r"authori[sz]ed to work in the (?:us|united states))\b[^.\n;]*",
    re.I,
)
FIT = re.compile(r"\b(automation|implementation|project|program|operations|workflow|ai)\b", re.I)


def fake_route(system: str, prompt: str, schema: dict) -> dict:
    jobs = json.loads(prompt[prompt.index("[") :])
    out = []
    for j in jobs:
        text = f"{j.get('location_field', '')}\n{j.get('description', '')}"
        employer = j.get("text_source") == "employer"
        quote, verdict = "", "UNCLEAR"
        if m := EXCL.search(text):
            quote, verdict = m.group(0).strip(), "EXCLUDED"
        elif (m := KENYA.search(text)) and employer:
            quote, verdict = m.group(0).strip(), "KENYA_CONFIRMED"
        elif (m := WIDE.search(text)) and employer:
            quote, verdict = m.group(0).strip(), "AFRICA_OR_WORLDWIDE_NO_EXCLUSION"
        fit = "STRONG" if FIT.search(j.get("title") or "") else "PARTIAL"
        route = {"EXCLUDED": "SKIP", "UNCLEAR": "CHECK_ELIGIBILITY"}.get(verdict, "APPLY")
        remote = re.search(r"\bremote\b", text, re.I)
        out.append(
            {
                "job_id": j["job_id"],
                "route": route,
                "eligibility_verdict": verdict,
                "quoted_location_text": quote,
                "function_match": fit,
                "seniority_match": "MATCH",
                "workplace": "REMOTE" if remote else "UNCLEAR",
                "workplace_city": "",
                "hours_requirement": {
                    "employment": "PART_TIME"
                    if re.search(r"part[- ]time", text, re.I)
                    else "UNCLEAR",
                    "hours_per_week": 0,
                    "us_hours_required": bool(re.search(r"\b(EST|PST|US hours)\b", text)),
                    "timezone_note": "",
                },
                "reasons": [f"fake router: keyword verdict {verdict}"],
            }
        )
    return {"jobs": out}


def _minimal(schema: dict):
    """Smallest value that satisfies a JSON schema (first enum value, empty string, ...)."""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        return {k: _minimal(v) for k, v in schema.get("properties", {}).items()}
    return {"array": [], "string": "", "number": 0, "integer": 0, "boolean": False}.get(t)


def fake_dispatch(system: str, prompt: str, schema: dict) -> dict:
    """Routing prompts get the keyword router; anything else gets a minimal valid answer."""
    if "jobs" in schema.get("properties", {}) and "[" in prompt:
        return fake_route(system, prompt, schema)
    return _minimal(schema)
