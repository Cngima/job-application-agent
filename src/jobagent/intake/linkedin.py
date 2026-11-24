"""Parse LinkedIn job alert emails (plain-text part) into leads.

Never fetches linkedin.com: everything comes from the email Gmail already holds. Each job
card in the email is a block of lines ending in "View job: <url>", separated by dashed lines:

    Technical Project Manager
    Example Ltd
    Nairobi
    Apply with resume & profile          <- optional noise lines
    View job: https://www.linkedin.com/comm/jobs/view/1234567890/?trackingId=...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

VIEW_JOB = re.compile(r"View job:\s*(https?://\S*linkedin\.com\S*?/jobs/view/(\d+)\S*)", re.I)
BLOCK_SEP = re.compile(r"^-{10,}\s*$", re.M)
NOISE = re.compile(
    r"^(apply with resume|easy apply|this company is actively hiring|actively recruiting|"
    r"be an early applicant|promoted|your job alert for|jobs similar to|new jobs similar to|"
    r"\d+ (company )?alumni|\d+ connections?|you may be a fit|top applicant|"
    r"see all jobs|manage your job alerts|view job)",
    re.I,
)


@dataclass(frozen=True)
class LinkedInLead:
    job_id: str
    title: str
    company: str
    location: str | None
    received_at: datetime | None = None

    @property
    def url(self) -> str:
        """Clean public job URL, tracking parameters removed. Stored, never fetched."""
        return f"https://www.linkedin.com/jobs/view/{self.job_id}/"


def parse_alert(text: str, received_at: datetime | None = None) -> list[LinkedInLead]:
    leads: list[LinkedInLead] = []
    seen: set[str] = set()
    for block in BLOCK_SEP.split(text.replace("\r\n", "\n")):
        m = VIEW_JOB.search(block)
        if not m:
            continue
        job_id = m.group(2)
        lines = [
            ln.strip()
            for ln in block[: m.start()].split("\n")
            if ln.strip() and "http" not in ln and not NOISE.match(ln.strip())
        ]
        if len(lines) < 2 or job_id in seen:
            continue
        seen.add(job_id)
        if len(lines) >= 3:
            title, company, location = lines[-3], lines[-2], lines[-1]
        else:
            title, company, location = lines[-2], lines[-1], None
        leads.append(LinkedInLead(job_id, title, company, location, received_at))
    return leads
