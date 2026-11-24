"""Stage 1 tests: CV parsing, evidence bank building and the sample bank."""

from __future__ import annotations

from pathlib import Path

import docx
import pytest
from typer.testing import CliRunner

from jobagent.cli import app
from jobagent.evidence.builder import (
    DEFAULT_NEVER_CLAIM,
    build_bank,
    load_bank,
    never_claim_hits,
    tools_in,
    write_bank,
)
from jobagent.evidence.parser import (
    clean_employer,
    parse_cv,
    parse_role_header,
    split_tools,
    to_ym,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------- parser units


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ("Sept 2019", "2019-09"),
        ("Jun 2026", "2026-06"),
        ("April 2016", "2016-04"),
        ("2014", "2014"),
        ("Present", "present"),
    ],
)
def test_to_ym(point: str, expected: str) -> None:
    assert to_ym(point) == expected


def test_role_header_new_layout() -> None:
    r = parse_role_header(
        "Team Lead, Project Coordinator  |  Assent Inc.  •  Ottawa, Canada (Remote)"
        "\tFeb 2024 – Oct 2025"
    )
    assert r is not None
    assert (r.title, r.employer, r.start, r.end) == (
        "Team Lead, Project Coordinator",
        "Assent Inc.",
        "2024-02",
        "2025-10",
    )


def test_role_header_old_layout_has_no_employer() -> None:
    r = parse_role_header("Program Success Analyst          Sept 2019 – Nov 2021")
    assert r is not None
    assert r.title == "Program Success Analyst" and r.employer is None


def test_role_header_one_liner_with_location() -> None:
    r = parse_role_header(
        "Earlier role: Office Administration Assistant, Premier Rehab Centre, Nairobi, Kenya | "
        "May 2016 – Oct 2017"
    )
    assert r is not None
    assert (r.title, r.employer) == ("Office Administration Assistant", "Premier Rehab Centre")


def test_clean_employer_strips_location() -> None:
    assert clean_employer("Assent Inc., Ottawa, Canada (Remote)") == "Assent Inc."
    assert clean_employer("Assent Inc. (Kenya Office)") == "Assent Inc."
    assert (
        clean_employer("Kompato AI / Trusting Social  •  Remote") == "Kompato AI / Trusting Social"
    )


def test_split_tools() -> None:
    body = (
        "Claude (AI agents & skills) | ChatGPT / Custom GPTs | Box – Cloud Content"
        " | MS – Office Suite"
    )
    assert split_tools(body) == ["Claude", "ChatGPT", "Custom GPTs", "Box", "MS Office Suite"]
    assert split_tools("Asana & ClickUp | GitHub Issues & Projects") == [
        "Asana",
        "ClickUp",
        "GitHub Issues & Projects",
    ]


def test_tools_in_is_case_sensitive() -> None:
    text = "Ran Zoom calls, then had to zoom in on a word in a Word file."
    assert tools_in(text, set()) == ["Microsoft Word", "Zoom"]
    assert tools_in("drafted in word, then zoomed", set()) == []


def test_parse_cv_old_layout_labels_and_employer_line() -> None:
    lines = [
        "AMINA WEKESA",
        "+254 700 000 000",
        "PROFESSIONAL PROFILE",
        "Coordinator with 5 years of experience. Calm under pressure.",
        "RELEVANT PROFESSIONAL EXPERIENCE",
        "Operations Coordinator          Jan 2020 – Feb 2023",
        "Sunbird Health Clinics",
        "Key Achievements:",
        "Trained 20 front-desk staff.",
        "Core Responsibilities:",
        "Tracked supplier deliveries in Trello.",
        "REFERENCES",
        "Available on request",
    ]
    cv = parse_cv(lines, "a.docx")
    assert cv.name == "Amina Wekesa"
    assert cv.profile == ["Coordinator with 5 years of experience.", "Calm under pressure."]
    (role,) = cv.roles
    assert role.employer == "Sunbird Health Clinics"
    assert role.bullets == [
        "Trained 20 front-desk staff.",
        "Tracked supplier deliveries in Trello.",
    ]


# ---------- builder, using real .docx files generated on the fly


def _make_cv(path: Path, bullets: list[str], title: str = "Automation Project Manager") -> Path:
    d = docx.Document()
    for line in [
        "AMINA WEKESA",
        "+254 700 000 000  |  amina@example.com  |  Kisumu, Kenya",
        "PROFESSIONAL PROFILE",
        "Automation lead with 6 years in operations.",
        "SKILLS SNAPSHOT",
        "Technical Skills: Zapier | Airtable | HubSpot",
        "PROFESSIONAL EXPERIENCE",
        f"{title}  |  Lakeside Logistics  •  Remote\tMar 2023 – Present",
        *bullets,
        "LICENSES & CERTIFICATIONS",
        "◆  Certified Associate in Project Management (CAPM), PMI",
    ]:
        d.add_paragraph(line)
    d.save(path)
    return path


def test_build_bank_merges_across_cvs(tmp_path: Path) -> None:
    a = _make_cv(
        tmp_path / "cv_a.docx",
        [
            "Built Zapier workflows that cut data entry by 12 hours per week.",
            "Ran weekly status meetings for the integrations team — every Monday.",
        ],
    )
    b = _make_cv(
        tmp_path / "cv_b.docx",
        [
            "Built Zapier workflows that cut data entry by 12 hours per week",  # near-duplicate
            "Configured ClickUp workspaces for the whole company.",
        ],
        title="AI Project Manager",
    )
    bank, report = build_bank([a, b])

    zap = next(f for f in bank.facts if "Zapier" in f.text)
    assert zap.source_count == 2
    assert zap.tools == ["Zapier"]
    assert "12 hours" in zap.metrics
    assert all("—" not in f.text for f in bank.facts), "em dashes must be stripped"

    (role,) = bank.roles
    assert role.title in {"Automation Project Manager", "AI Project Manager"}
    assert len(role.title_variants) == 2
    assert "AI Project Manager" in report  # title variant surfaced for review

    assert {"Zapier", "Airtable", "HubSpot"} <= bank.tool_names()
    assert bank.contact.email == "amina@example.com"
    assert "ClickUp as deep configuration" in report  # never_claim conflict surfaced


def test_never_claim_patterns() -> None:
    assert never_claim_hits("Implemented SAP S/4HANA", DEFAULT_NEVER_CLAIM) == ["ERP systems"]
    assert never_claim_hits("Prioritised with RICE scoring", DEFAULT_NEVER_CLAIM)
    assert not never_claim_hits("Bought rice for the team lunch", DEFAULT_NEVER_CLAIM)
    assert never_claim_hits("Used ClickUp daily", DEFAULT_NEVER_CLAIM) == []
    assert never_claim_hits("ClickUp configuration for 40 users", DEFAULT_NEVER_CLAIM)


def test_bank_round_trip(tmp_path: Path) -> None:
    bank, _ = build_bank([_make_cv(tmp_path / "cv.docx", ["Did a thing with Airtable."])])
    out = tmp_path / "bank.yaml"
    write_bank(bank, out)
    assert load_bank(out) == bank


def test_cli_never_overwrites_reviewed_bank(tmp_path: Path) -> None:
    _make_cv(tmp_path / "cv.docx", ["Did a thing with Airtable."])
    out = tmp_path / "evidence_bank.yaml"
    out.write_text("reviewed: true\n", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["evidence", "build", "--cv-dir", str(tmp_path), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == "reviewed: true\n"
    assert (tmp_path / "evidence_bank.new.yaml").exists()


# ---------- the public sample bank


def test_sample_bank_is_valid_and_fictional() -> None:
    bank = load_bank(ROOT / "data/sample/evidence_bank.yaml")
    assert bank.contact.name == "Amina Wekesa"
    assert bank.contact.email.endswith("@example.com")
    assert len(bank.facts) >= 8
    for f in bank.facts:
        for tool in f.tools:
            assert tool in bank.tool_names(), f"{f.id} uses {tool} not in allowed_tools"


# ---------- review overrides


def test_overrides_apply_titles_years_and_credentials(tmp_path: Path) -> None:
    from jobagent.evidence.overrides import Overrides, RoleOverride, apply_overrides

    a = _make_cv(tmp_path / "a.docx", ["Built Zapier workflows."], title="AI Project Manager")
    b = _make_cv(tmp_path / "b.docx", ["Built Zapier workflows."], title="Sales Project Manager")
    bank, _ = build_bank([a, b])
    ov = Overrides(
        roles={
            "2023-03": RoleOverride(
                title="Automation Project Manager", allowed_titles=["AI Project Manager"]
            )
        },
        years_of_experience="10+",
        credentials_keep=["Certified Associate in Project Management (CAPM), PMI"],
        tools_remove=["HubSpot"],
    )
    out, changes = apply_overrides(bank, ov)

    (role,) = out.roles
    assert role.title == "Automation Project Manager"
    assert [tv.title for tv in role.title_variants] == ["AI Project Manager"]  # Sales rejected
    assert all(f.role == "Automation Project Manager" for f in out.facts if f.kind == "experience")
    # "6 years" profile sentence dropped, one owner fact added
    assert not any("6 years" in f.text for f in out.facts)
    assert out.fact("owner-years").text == "10+ years of professional experience."
    assert "HubSpot" not in out.tool_names()
    assert len(out.credentials) == 1
    assert changes


def test_cli_build_applies_overrides_file(tmp_path: Path) -> None:
    _make_cv(tmp_path / "cv.docx", ["Did a thing with Airtable."])
    ov = tmp_path / "ov.yaml"
    ov.write_text('years_of_experience: "10+"\n', encoding="utf-8")
    out = tmp_path / "bank.yaml"
    result = CliRunner().invoke(
        app,
        ["evidence", "build", "--cv-dir", str(tmp_path), "--out", str(out), "--overrides", str(ov)],
    )
    assert result.exit_code == 0, result.output
    assert load_bank(out).fact("owner-years") is not None
    assert "Review decisions applied" in (tmp_path / "bank_review.md").read_text(encoding="utf-8")


def test_overrides_add_role_projects_and_owner_tools(tmp_path: Path) -> None:
    from jobagent.evidence.overrides import Overrides, apply_overrides

    bank, _ = build_bank([_make_cv(tmp_path / "a.docx", ["Built Zapier workflows."])])
    ov = Overrides.model_validate(
        {
            "contact": {"github": "https://github.com/example"},
            "roles_add": [
                {
                    "role": {
                        "key": "2021",
                        "title": "Founder",
                        "employer": "Side Biz",
                        "start": "2021",
                        "end": "present",
                        "employment_type": "part_time",
                        "tools": ["Streamlit"],
                        "constraints": ["Mark as part-time."],
                    },
                    "facts": [
                        {
                            "id": "biz-1",
                            "kind": "experience",
                            "text": "Built a thing.",
                            "date": "2026",
                            "tools": ["Make"],
                        }
                    ],
                }
            ],
            "ai_automation_projects": [
                {
                    "id": "p1",
                    "name": "Thing",
                    "link": "https://github.com/x/thing",
                    "description": "Does one thing.",
                    "tools": ["Python"],
                },
                {
                    "id": "p2",
                    "name": "Later",
                    "link": "https://github.com/x/later",
                    "status": "pending",
                },
            ],
        }
    )
    out, changes = apply_overrides(bank, ov)

    role = next(r for r in out.roles if r.employer == "Side Biz")
    assert role.employment_type == "part_time" and role.constraints == ["Mark as part-time."]
    fact = out.fact("biz-1")
    assert (fact.role, fact.employer, fact.start, fact.date) == (
        "Founder",
        "Side Biz",
        "2021",
        "2026",
    )
    assert fact.sources == ["owner_review"]
    assert {"Streamlit", "Make", "Python"} <= out.tool_names()  # owner-named tools allowed
    assert [p.status for p in out.ai_automation_projects] == ["ready", "pending"]
    assert out.contact.github == "https://github.com/example"
    assert [r.start for r in out.roles] == sorted([r.start for r in out.roles], reverse=True)


def test_duplicate_ids_across_projects_and_facts_rejected(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from jobagent.evidence.overrides import Overrides, apply_overrides

    bank, _ = build_bank([_make_cv(tmp_path / "a.docx", ["Built Zapier workflows."])])
    ov = Overrides.model_validate(
        {
            "facts_add": [{"id": "dup", "kind": "profile", "text": "x"}],
            "ai_automation_projects": [{"id": "dup", "name": "n", "link": "https://x"}],
        }
    )
    with pytest.raises(ValidationError):
        apply_overrides(bank, ov)
