"""Pydantic models for the evidence bank.

The evidence bank is the only source of truth for anything the agent may say about the
candidate. Every generated document is validated against it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

FactKind = Literal["experience", "achievement", "project", "profile"]


class Fact(BaseModel):
    """One atomic, citable statement taken verbatim from a CV."""

    id: str
    kind: FactKind
    text: str
    heading: str | None = None  # achievement or project title, when the CV gives one
    role: str | None = None
    employer: str | None = None
    start: str | None = None  # "YYYY-MM"
    end: str | None = None  # "YYYY-MM" or "present"
    tools: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    source_count: int = 0


class TitleVariant(BaseModel):
    title: str
    sources: list[str]


class Role(BaseModel):
    """A position, grouped across CVs by start month."""

    key: str
    title: str  # most common title across CVs
    employer: str
    start: str | None
    end: str | None
    title_variants: list[TitleVariant] = Field(default_factory=list)
    employer_variants: list[str] = Field(default_factory=list)
    source_count: int = 0


class Credential(BaseModel):
    id: str
    kind: Literal["education", "certification", "membership", "language", "other"]
    text: str
    sources: list[str] = Field(default_factory=list)
    source_count: int = 0


class AllowedTool(BaseModel):
    name: str
    source_count: int
    sources: list[str] = Field(default_factory=list)


class NeverClaim(BaseModel):
    """Something the candidate must never claim. `patterns` are case-insensitive regexes."""

    claim: str
    patterns: list[str]
    note: str = ""


class Contact(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    location: str | None = None


class Preferences(BaseModel):
    remote_only: bool = True
    part_time_ok: bool = True
    based_in: str = "Kenya"
    timezone: str = "Africa/Nairobi (UTC+3)"
    notes: list[str] = Field(default_factory=list)


class EvidenceBank(BaseModel):
    version: int = 1
    generated_from: list[str] = Field(default_factory=list)
    contact: Contact
    preferences: Preferences = Field(default_factory=Preferences)
    roles: list[Role] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    competencies: list[str] = Field(default_factory=list)
    credentials: list[Credential] = Field(default_factory=list)
    allowed_tools: list[AllowedTool] = Field(default_factory=list)
    never_claim: list[NeverClaim] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> EvidenceBank:
        ids = [f.id for f in self.facts] + [c.id for c in self.credentials]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate ids in evidence bank: {sorted(dupes)}")
        return self

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    def tool_names(self) -> set[str]:
        return {t.name for t in self.allowed_tools}
