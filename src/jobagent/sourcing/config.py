"""Models for config/sources.yaml and the parts of config/search_profile.yaml sourcing uses."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from jobagent.sourcing.models import SourceKind

ADAPTERS = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "breezy",
    "zoho",
    "remoteok",
    "himalayas",
    "jobicy",
    "myjobmag",
    "reddit",
}


class SourceConfig(BaseModel):
    name: str
    adapter: str
    kind: SourceKind
    slug: str | None = None  # board token for ATS adapters
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    # Only for sources whose robots.txt Carol has explicitly decided does not apply.
    robots_override_reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> SourceConfig:
        if self.adapter not in ADAPTERS:
            raise ValueError(f"{self.name}: unknown adapter {self.adapter!r}")
        ats = {"greenhouse", "lever", "ashby", "workable", "breezy", "zoho"}
        if self.adapter in ats and not self.slug:
            raise ValueError(f"{self.name}: {self.adapter} needs a slug")
        return self


class WatchEntry(BaseModel):
    name: str
    url: str | None = None
    why: str


class UnsupportedEntry(BaseModel):
    name: str
    reason: str


class SourcesFile(BaseModel):
    sources: list[SourceConfig]
    manual_watch: list[WatchEntry] = Field(default_factory=list)
    not_supported: list[UnsupportedEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> SourcesFile:
        names = [s.name for s in self.sources]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate source names: {sorted(dupes)}")
        return self


def load_sources(path: Path | str = "config/sources.yaml") -> SourcesFile:
    with open(path, encoding="utf-8") as fh:
        return SourcesFile.model_validate(yaml.safe_load(fh))


class TitlePrefilter(BaseModel):
    """Cheap title screen used before fetching job details from very large boards."""

    include: list[str]
    exclude: list[str] = Field(default_factory=list)

    def _rx(self, terms: list[str]) -> re.Pattern[str] | None:
        if not terms:
            return None
        return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)

    def passes(self, title: str) -> bool:
        inc, exc = self._rx(self.include), self._rx(self.exclude)
        if exc and exc.search(title):
            return False
        return bool(inc and inc.search(title))


class SearchProfile(BaseModel):
    titles: list[str] = Field(default_factory=list)
    priority_keywords: list[str] = Field(default_factory=list)
    title_prefilter: TitlePrefilter
    max_age_days: int | None = 7  # drop postings older than this (None = no limit)
    kenya_onsite_ok: bool = False
    kenya_hybrid_cities: list[str] = Field(default_factory=lambda: ["Nairobi", "Nakuru"])
    model_config = {"extra": "allow"}


def load_search_profile(path: Path | str = "config/search_profile.yaml") -> SearchProfile:
    with open(path, encoding="utf-8") as fh:
        return SearchProfile.model_validate(yaml.safe_load(fh))
