"""The single normalised Job model every source adapter produces."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from jobagent.sourcing.text import canonical_url, dedupe_key

SourceKind = Literal["employer_ats", "agency", "discovery", "community", "manual"]

# Where the description text came from. Only "employer" text may prove eligibility.
TextOrigin = Literal["employer", "aggregator", "community", "pasted"]


class Job(BaseModel):
    source: str  # config name of the source, e.g. "greenhouse:gitlab", "himalayas"
    source_kind: SourceKind
    title: str
    company: str
    url: str  # the page this job was found on
    description: str = ""  # plain text
    text_origin: TextOrigin = "employer"
    employer_url: str | None = None  # the employer's own posting, when known
    apply_url: str | None = None
    external_id: str | None = None
    location_text: str | None = None  # location as stated on the employer's own posting
    aggregator_location: str | None = None  # an aggregator's label: stored, never trusted
    employment_type: str | None = None
    remote: bool | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None  # year | month | week | day | hour
    salary_text: str | None = None
    posted_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tags: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)  # e.g. scam_risk, evergreen
    attribution: str | None = None  # credit required by the source's terms

    @computed_field  # type: ignore[prop-decorator]
    @property
    def canonical_url(self) -> str:
        return canonical_url(self.employer_url or self.url)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.title, self.company)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return hashlib.sha1(self.canonical_url.encode()).hexdigest()[:16]
