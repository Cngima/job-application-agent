"""Apply the owner's review decisions to a freshly built evidence bank.

Review decisions live in a small YAML file (data/private/evidence_overrides.yaml) instead of
hand edits to the generated bank. That makes the bank reproducible: rebuilding from the CVs
and re-applying the overrides always gives the same reviewed result.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from jobagent.evidence.models import (
    AIProject,
    AllowedTool,
    Credential,
    EvidenceBank,
    Fact,
    Role,
    TitleVariant,
)

log = logging.getLogger(__name__)

OWNER = "owner_review"
YEARS_RE = re.compile(r"\b(?:over\s+)?(\d+)\+?\s+years\b", re.IGNORECASE)


class RoleOverride(BaseModel):
    title: str | None = None
    employer: str | None = None
    end: str | None = None
    allowed_titles: list[str] = Field(default_factory=list)  # approved alternates
    employer_variants: list[str] | None = None
    constraints: list[str] | None = None
    banned_terms: list[str] | None = None
    preferred_facts: list[str] | None = None


class NewRole(BaseModel):
    """A role supplied by the owner that does not appear in any CV."""

    role: Role
    facts: list[Fact] = Field(default_factory=list)


class Overrides(BaseModel):
    reviewed_on: str | None = None
    roles: dict[str, RoleOverride] = Field(default_factory=dict)  # keyed by role start "YYYY-MM"
    roles_add: list[NewRole] = Field(default_factory=list)
    years_of_experience: str | None = None  # e.g. "10+"
    credentials_keep: list[str] | None = None  # exact texts; others are dropped
    tools_add: list[str] = Field(default_factory=list)
    tools_remove: list[str] = Field(default_factory=list)
    facts_remove: list[str] = Field(default_factory=list)
    facts_add: list[Fact] = Field(default_factory=list)
    ai_automation_projects: list[AIProject] = Field(default_factory=list)
    contact: dict[str, str] = Field(default_factory=dict)


def load_overrides(path: Path | str) -> Overrides:
    with open(path, encoding="utf-8") as fh:
        return Overrides.model_validate(yaml.safe_load(fh) or {})


def apply_overrides(bank: EvidenceBank, ov: Overrides) -> tuple[EvidenceBank, list[str]]:
    """Return a new bank with decisions applied, plus a log of what changed."""
    bank = bank.model_copy(deep=True)
    changes: list[str] = []

    # Roles: canonical title, approved alternates only, fixed end dates.
    for role in bank.roles:
        o = ov.roles.get(role.key)
        if not o:
            continue
        old_title = role.title
        if o.title:
            role.title = o.title
        if o.employer:
            role.employer = o.employer
        if o.end:
            role.end = o.end
        seen = {tv.title: tv for tv in role.title_variants}
        role.title_variants = [
            seen.get(t) or TitleVariant(title=t, sources=[OWNER]) for t in o.allowed_titles
        ]
        if o.employer_variants is not None:
            role.employer_variants = o.employer_variants
        for attr in ("constraints", "banned_terms", "preferred_facts"):
            if getattr(o, attr) is not None:
                setattr(role, attr, getattr(o, attr))
        for f in bank.facts:
            if f.start == role.key and f.kind == "experience":
                f.role, f.employer, f.end = role.title, role.employer, role.end
        changes.append(
            f"role {role.key}: '{old_title}' -> '{role.title}', end {role.end}, "
            f"alternates {o.allowed_titles or 'none'}"
        )

    # Years of experience: one owner-confirmed figure replaces conflicting summary claims.
    if ov.years_of_experience:
        target = ov.years_of_experience.rstrip("+")
        drop = [
            f.id
            for f in bank.facts
            if f.kind == "profile" and any(m.group(1) != target for m in YEARS_RE.finditer(f.text))
        ]
        bank.facts = [f for f in bank.facts if f.id not in drop]
        bank.facts.append(
            Fact(
                id="owner-years",
                kind="profile",
                text=f"{ov.years_of_experience} years of professional experience.",
                metrics=[f"{ov.years_of_experience} years"],
                sources=[OWNER],
                source_count=1,
            )
        )
        changes.append(
            f"years: kept {ov.years_of_experience}, dropped {len(drop)} profile "
            f"sentences with other totals"
        )

    if ov.credentials_keep is not None:
        keep = {_squash(t) for t in ov.credentials_keep}
        before = len(bank.credentials)
        bank.credentials = [c for c in bank.credentials if _squash(c.text) in keep]
        missing = keep - {_squash(c.text) for c in bank.credentials}
        for text in ov.credentials_keep:
            if _squash(text) in missing:
                bank.credentials.append(
                    Credential(
                        id=f"owner-cred-{len(bank.credentials)}",
                        kind="other",
                        text=text,
                        sources=[OWNER],
                        source_count=1,
                    )
                )
        changes.append(f"credentials: {before} -> {len(bank.credentials)}")

    if ov.tools_remove:
        gone = set(ov.tools_remove)
        bank.allowed_tools = [t for t in bank.allowed_tools if t.name not in gone]
        for f in bank.facts:
            f.tools = [t for t in f.tools if t not in gone]
        changes.append(f"tools removed: {sorted(gone)}")

    if ov.facts_remove:
        gone = set(ov.facts_remove)
        bank.facts = [f for f in bank.facts if f.id not in gone]
        changes.append(f"facts removed: {len(gone)}")

    for f in ov.facts_add:
        if not f.sources:
            f.sources, f.source_count = [OWNER], 1
        bank.facts.append(f)
    if ov.facts_add:
        changes.append(f"facts added: {[f.id for f in ov.facts_add]}")

    for nr in ov.roles_add:
        role = nr.role.model_copy(update={"source_count": nr.role.source_count or 1})
        bank.roles.append(role)
        for f in nr.facts:
            bank.facts.append(
                f.model_copy(
                    update={
                        "kind": f.kind or "experience",
                        "role": f.role or role.title,
                        "employer": f.employer or role.employer,
                        "start": f.start or role.start,
                        "end": f.end or role.end,
                        "sources": f.sources or [OWNER],
                        "source_count": f.source_count or 1,
                    }
                )
            )
        changes.append(f"role added: {role.title} | {role.employer} ({len(nr.facts)} facts)")
    if ov.roles_add:
        bank.roles.sort(key=lambda r: r.start or "", reverse=True)

    if ov.ai_automation_projects:
        bank.ai_automation_projects = list(ov.ai_automation_projects)
        ready = sum(p.status == "ready" for p in ov.ai_automation_projects)
        changes.append(
            f"AI & automation projects: {ready} ready, "
            f"{len(ov.ai_automation_projects) - ready} pending"
        )

    # Every tool named by the owner (explicitly, on a role, fact or project) becomes allowed.
    owner_tools = set(ov.tools_add)
    for nr in ov.roles_add:
        owner_tools.update(nr.role.tools)
        for f in nr.facts:
            owner_tools.update(f.tools)
    for p in ov.ai_automation_projects:
        owner_tools.update(p.tools)
    added = sorted(owner_tools - bank.tool_names())
    for name in added:
        bank.allowed_tools.append(AllowedTool(name=name, source_count=1, sources=[OWNER]))
    if added:
        changes.append(f"tools added by owner: {added}")

    if ov.contact:
        bank.contact = bank.contact.model_copy(update=ov.contact)
        changes.append(f"contact updated: {sorted(ov.contact)}")

    return EvidenceBank.model_validate(bank.model_dump()), changes


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()
