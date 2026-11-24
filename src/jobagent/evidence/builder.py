"""Build an evidence bank from many CVs.

Steps: parse each CV, group roles by start month, merge near-duplicate bullets across CVs,
tag tools and metrics, and write YAML plus a Markdown review report for the human to check.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from jobagent.evidence.docx_reader import read_lines
from jobagent.evidence.models import (
    AllowedTool,
    Contact,
    Credential,
    EvidenceBank,
    Fact,
    NeverClaim,
    Preferences,
    Role,
    TitleVariant,
)
from jobagent.evidence.parser import ParsedCV, parse_cv

log = logging.getLogger(__name__)

MERGE_RATIO = 0.90

# Tools recognised inside bullet text. Skills-line entries are always included as well.
# Keys are regexes, values are canonical names.
TOOL_VOCAB: dict[str, str] = {
    r"\bHubSpot\b": "HubSpot",
    r"\bSalesforce\b": "Salesforce",
    r"\bClay\b": "Clay",
    r"\bApollo\b": "Apollo",
    r"\bPhantomBuster\b": "PhantomBuster",
    r"\b(Google )?Apps Script\b": "Google Apps Script",
    r"\bGoogle Sheets\b": "Google Sheets",
    r"\bJira\b": "Jira",
    r"\bConfluence\b": "Confluence",
    r"\bClickUp\b": "ClickUp",
    r"\bAsana\b": "Asana",
    r"\bTrello\b": "Trello",
    r"\bmonday\.com\b": "monday.com",
    r"\bSmartsheet( Dashboards)?\b": "Smartsheet",
    r"\bPower BI\b": "Power BI",
    r"\bTableau\b": "Tableau",
    r"\bExcel\b": "Excel",
    r"\bGoogle Workspace\b": "Google Workspace",
    r"\bSlack\b": "Slack",
    r"\bMicrosoft Teams\b": "Microsoft Teams",
    r"\bZoom\b": "Zoom",
    r"\b(Microsoft )?SharePoint\b": "SharePoint",
    r"\bClaude\b": "Claude",
    r"\bChatGPT\b": "ChatGPT",
    r"(?i)\bcustom GPTs?\b": "Custom GPTs",
    r"\b(Google )?NotebookLM\b": "NotebookLM",
    r"\bZapier\b": "Zapier",
    r"\bMake\.com\b|\bMake \(Integromat\)": "Make",
    r"\bn8n\b": "n8n",
    r"\bNotion\b": "Notion",
    r"\bAirtable\b": "Airtable",
    r"\bZendesk\b": "Zendesk",
    r"\bIntercom\b": "Intercom",
    r"\bFreshdesk\b": "Freshdesk",
    r"\bLinkedIn Ads\b": "LinkedIn Ads",
    r"\bGoogle Ads\b": "Google Ads",
    r"\bLooker Studio\b": "Looker Studio",
    r"\bSQL\b": "SQL",
    r"\bPython\b": "Python",
    r"\bPostman\b": "Postman",
    r"\bGemini\b": "Gemini",
    r"\bCopilot\b": "Copilot",
    r"\bMiro\b": "Miro",
    r"\bLucidchart\b": "Lucidchart",
    r"\bFigma\b": "Figma",
    r"\bCanva\b": "Canva",
    r"\bLoom\b": "Loom",
    r"\bSFTP\b|Secure File Transfer Protocol": "SFTP",
    r"\bREST APIs?\b": "REST APIs",
    r"(?i)\bwebhooks?\b": "Webhooks",
    r"\bJSON\b": "JSON",
    r"\bSpark Hire\b": "Spark Hire",
    r"\bClaude Code\b": "Claude Code",
    r"\bClaude Cowork\b|\bCowork\b": "Claude Cowork",
    r"\bMCP\b": "MCP",
    r"\bCursor\b": "Cursor",
    r"\bGranola\b": "Granola",
    r"\bGitHub( Issues)?\b": "GitHub",
    r"\bGuru\b": "Guru",
    r"\bForethought\b": "Forethought",
    r"\bFinancialForce\b": "FinancialForce",
    r"\bService Cloud\b": "Salesforce Service Cloud",
    r"\bGmail\b": "Gmail",
    r"\bGoogle Drive\b": "Google Drive",
    r"\bMicrosoft Office\b|\bMS[ -]+Office( Suite)?\b": "Microsoft Office",
    r"\bMicrosoft 365\b": "Microsoft 365",
    r"\bMarkdown\b": "Markdown",
    r"\b(Microsoft )?Word\b": "Microsoft Word",
}

METRIC_RE = re.compile(
    r"(?:US)?[$€£]\s?\d[\d,.]*\s?[KkMm]?"  # money
    r"|\b\d[\d,.]*\s?%"  # percentages
    r"|\b\d[\d,.]*\+?\s?(?:-\s?)?[A-Za-z][A-Za-z-]*"  # '19 client', '8+ years', '23-section'
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)[- ]"
    r"(?:client|phase|pair|hours?|weeks?|days?|section)\w*",
    re.IGNORECASE,
)

DEFAULT_NEVER_CLAIM: list[NeverClaim] = [
    NeverClaim(
        claim="ERP systems",
        patterns=[
            r"\bERP\b",
            r"enterprise resource planning",
            r"(?-i:\bSAP\b)",
            r"\bNetSuite\b",
            r"\bOdoo\b",
            r"Dynamics 365",
        ],
        note="No ERP implementation or administration experience.",
    ),
    NeverClaim(claim="Codex", patterns=[r"\bCodex\b"]),
    NeverClaim(
        claim="ClickUp as deep configuration",
        patterns=[
            r"ClickUp\s+(admin\w*|configur\w*|architect\w*|implementation|build\w*)",
            r"(configur|architect|administer|implement)\w*\s+(?:\w+\s+){0,3}ClickUp",
        ],
        note="ClickUp used as a user only.",
    ),
    NeverClaim(
        claim="CAT tools",
        patterns=[
            r"\bCAT tools?\b",
            r"computer[- ]assisted translation",
            r"\bTrados\b",
            r"\bmemoQ\b",
        ],
    ),
    NeverClaim(claim="DNS", patterns=[r"\bDNS\b", r"domain name system"]),
    NeverClaim(
        claim="Procurement-to-pay",
        patterns=[r"procure(ment)?[- ]to[- ]pay", r"\bP2P\b"],
        note="The procurement degree is real; P2P process ownership is not.",
    ),
    NeverClaim(
        claim="Product management frameworks such as RICE and Kano",
        patterns=[r"(?-i:\bRICE\b)", r"\bKano\b"],
    ),
]


def normalise(text: str) -> str:
    text = text.lower().replace("–", "-").replace("—", "-")
    text = re.sub(r"[^a-z0-9%$+ ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_em_dashes(text: str) -> str:
    """House rule: no em dashes. ' — ' becomes ', ' and a bare '—' becomes '-'."""
    return re.sub(r"\s*—\s*", ", ", text) if " — " in text else text.replace("—", "-")


def _short_id(prefix: str, text: str) -> str:
    return f"{prefix}-{hashlib.sha1(normalise(text).encode()).hexdigest()[:6]}"


def _slug(text: str | None) -> str:
    if not text:
        return "gen"
    return re.sub(r"[^a-z0-9]", "", text.split()[0].lower())[:10] or "gen"


class _Cluster:
    """A group of near-identical lines seen across CVs."""

    def __init__(self, text: str, source: str) -> None:
        self.norm = normalise(text)
        self.variants: Counter[str] = Counter({text: 1})
        self.sources: set[str] = {source}

    def matches(self, norm: str) -> bool:
        if norm == self.norm:
            return True
        sm = SequenceMatcher(None, self.norm, norm, autojunk=False)
        return (
            sm.real_quick_ratio() >= MERGE_RATIO
            and sm.quick_ratio() >= MERGE_RATIO
            and (sm.ratio() >= MERGE_RATIO)
        )

    def add(self, text: str, source: str) -> None:
        self.variants[text] += 1
        self.sources.add(source)

    @property
    def text(self) -> str:
        # Most common phrasing; ties go to the longer (usually fuller) one.
        return max(self.variants.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def _cluster_into(clusters: list[_Cluster], text: str, source: str) -> None:
    norm = normalise(text)
    for cl in clusters:
        if cl.matches(norm):
            cl.add(text, source)
            return
    clusters.append(_Cluster(text, source))


def tools_in(text: str, extra_names: set[str]) -> list[str]:
    # Case-sensitive on purpose: "Zoom", "Word" and "Cursor" are tools, "zoom" and "word" are not.
    found = {name for pat, name in TOOL_VOCAB.items() if re.search(pat, text)}
    for name in extra_names:
        if len(name) > 2 and re.search(rf"\b{re.escape(name)}\b", text):
            found.add(name)
    return sorted(found)


def metrics_in(text: str) -> list[str]:
    return sorted({m.group(0).strip() for m in METRIC_RE.finditer(text)})


def _canonical_tool(name: str) -> str:
    for pat, canon in TOOL_VOCAB.items():
        if re.fullmatch(pat, name, re.IGNORECASE) or re.fullmatch(
            pat.replace(r"\b", ""), name, re.IGNORECASE
        ):
            return canon
    return name


def _contact(cvs: list[ParsedCV]) -> Contact:
    joined = "\n".join(line for cv in cvs for line in cv.header_lines)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", joined)
    phone = re.search(r"\+\d[\d ]{8,}\d", joined)
    linkedin = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+", joined)
    locs = Counter(
        m.group(0)
        for cv in cvs
        for line in cv.header_lines
        if (m := re.search(r"[A-Z][a-z]+, Kenya", line))
    )
    names = Counter(cv.name for cv in cvs if cv.name)
    return Contact(
        name=names.most_common(1)[0][0] if names else "Unknown",
        email=email.group(0) if email else None,
        phone=phone.group(0).strip() if phone else None,
        linkedin=linkedin.group(0) if linkedin else None,
        location=locs.most_common(1)[0][0] if locs else None,
    )


def build_bank(cv_paths: list[Path]) -> tuple[EvidenceBank, str]:
    """Return the evidence bank and a Markdown review report."""
    cvs = [parse_cv(read_lines(p), p.name) for p in cv_paths]
    log.info("parsed %d CVs", len(cvs))

    # ---- roles, grouped by start month
    role_groups: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for cv in cvs:
        for r in cv.roles:
            role_groups[r.start or r.title].append((cv.source, r))

    roles: dict[str, Role] = {}
    end_variants: dict[str, Counter[str]] = {}
    for key, items in role_groups.items():
        titles: dict[str, set[str]] = defaultdict(set)
        employers: Counter[str] = Counter()
        ends: Counter[str] = Counter()
        for src, r in items:
            titles[r.title].add(src)
            if r.employer:
                employers[r.employer] += 1
            ends[r.end or "?"] += 1
        title = max(titles.items(), key=lambda kv: len(kv[1]))[0]
        roles[key] = Role(
            key=key,
            title=title,
            employer=employers.most_common(1)[0][0] if employers else "UNKNOWN",
            start=items[0][1].start,
            end=ends.most_common(1)[0][0],
            title_variants=[
                TitleVariant(title=t, sources=sorted(s))
                for t, s in sorted(titles.items(), key=lambda kv: -len(kv[1]))
            ],
            employer_variants=sorted(employers),
            source_count=len({src for src, _ in items}),
        )
        end_variants[key] = ends

    skill_tools = {_canonical_tool(t) for cv in cvs for t in cv.tools}

    # ---- experience bullets, clustered per role
    facts: list[Fact] = []
    for key, items in role_groups.items():
        clusters: list[_Cluster] = []
        for src, r in items:
            for b in r.bullets:
                _cluster_into(clusters, b, src)
        role = roles[key]
        for cl in clusters:
            text = strip_em_dashes(cl.text)
            facts.append(
                Fact(
                    id=_short_id(_slug(role.employer), key + cl.norm),
                    kind="experience",
                    text=text,
                    role=role.title,
                    employer=role.employer,
                    start=role.start,
                    end=role.end,
                    tools=tools_in(text, skill_tools),
                    metrics=metrics_in(text),
                    sources=sorted(cl.sources),
                    source_count=len(cl.sources),
                )
            )

    # ---- achievements, projects, profile sentences
    ach_clusters: dict[tuple[str, str | None], list[_Cluster]] = defaultdict(list)
    for cv in cvs:
        for heading, employer, text in cv.achievements:
            _cluster_into(ach_clusters[(heading, employer)], text, cv.source)
    for (heading, employer), cls in ach_clusters.items():
        for cl in cls:
            text = strip_em_dashes(cl.text)
            facts.append(
                Fact(
                    id=_short_id("ach", heading + cl.norm),
                    kind="achievement",
                    text=text,
                    heading=heading,
                    employer=employer,
                    tools=tools_in(text, skill_tools),
                    metrics=metrics_in(text),
                    sources=sorted(cl.sources),
                    source_count=len(cl.sources),
                )
            )

    proj_clusters: list[_Cluster] = []
    proj_heading: dict[str, str] = {}
    for cv in cvs:
        for heading, text in cv.projects:
            _cluster_into(proj_clusters, text, cv.source)
            proj_heading.setdefault(normalise(text), heading)
    for cl in proj_clusters:
        text = strip_em_dashes(cl.text)
        facts.append(
            Fact(
                id=_short_id("proj", cl.norm),
                kind="project",
                text=text,
                heading=proj_heading.get(cl.norm),
                tools=tools_in(text, skill_tools),
                metrics=metrics_in(text),
                sources=sorted(cl.sources),
                source_count=len(cl.sources),
            )
        )

    prof_clusters: list[_Cluster] = []
    for cv in cvs:
        for s in cv.profile:
            _cluster_into(prof_clusters, s, cv.source)
    for cl in prof_clusters:
        text = strip_em_dashes(cl.text)
        facts.append(
            Fact(
                id=_short_id("prof", cl.norm),
                kind="profile",
                text=text,
                tools=tools_in(text, skill_tools),
                metrics=metrics_in(text),
                sources=sorted(cl.sources),
                source_count=len(cl.sources),
            )
        )

    # ---- credentials
    cred_clusters: dict[str, list[_Cluster]] = defaultdict(list)
    for cv in cvs:
        for kind, text in cv.credentials:
            _cluster_into(cred_clusters[kind], text, cv.source)
    credentials = [
        Credential(
            id=_short_id(f"cred{kind[:3]}", cl.norm),
            kind=kind,
            text=strip_em_dashes(cl.text),
            sources=sorted(cl.sources),
            source_count=len(cl.sources),
        )
        for kind, cls in cred_clusters.items()
        for cl in cls
    ]

    # ---- competencies and tools
    comp_counter = Counter(c for cv in cvs for c in dict.fromkeys(cv.competencies))
    competencies = [c for c, _ in comp_counter.most_common()]

    tool_sources: dict[str, set[str]] = defaultdict(set)
    for cv in cvs:
        for t in cv.tools:
            tool_sources[_canonical_tool(t)].add(cv.source)
    for f in facts:
        for t in f.tools:
            tool_sources[t].update(f.sources)
    allowed_tools = [
        AllowedTool(name=n, source_count=len(s), sources=sorted(s))
        for n, s in sorted(tool_sources.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    ]

    bank = EvidenceBank(
        generated_from=sorted(cv.source for cv in cvs),
        contact=_contact(cvs),
        preferences=Preferences(
            notes=[
                "Targeting (titles, pay floors, hours) lives in config/search_profile.yaml.",
            ]
        ),
        roles=sorted(roles.values(), key=lambda r: r.start or "", reverse=True),
        facts=facts,
        competencies=competencies,
        credentials=credentials,
        allowed_tools=allowed_tools,
        never_claim=DEFAULT_NEVER_CLAIM,
    )
    report = review_report(bank, cvs, end_variants)
    return bank, report


def never_claim_hits(text: str, never: list[NeverClaim]) -> list[str]:
    return [nc.claim for nc in never if any(re.search(p, text, re.IGNORECASE) for p in nc.patterns)]


def review_report(
    bank: EvidenceBank, cvs: list[ParsedCV], end_variants: dict[str, Counter[str]]
) -> str:
    n = len(cvs)
    out = [
        "# Evidence bank review",
        "",
        f"Built from {n} CVs: {len(bank.facts)} facts, {len(bank.roles)} roles, "
        f"{len(bank.credentials)} credentials, {len(bank.allowed_tools)} allowed tools.",
        "",
        "Check each section below, then edit `evidence_bank.yaml` directly. "
        "Delete anything untrue, fix titles and dates, and add never_claim items.",
        "",
        "## 1. Roles and title variants",
        "",
        "Tailored CVs sometimes retitle a role. Keep only titles you can defend in a reference "
        "check. The canonical title is the one used most often.",
        "",
    ]
    for r in bank.roles:
        out.append(f"### {r.title} | {r.employer} | {r.start} to {r.end} ({r.source_count} CVs)")
        if len(r.title_variants) > 1:
            for tv in r.title_variants:
                out.append(
                    f"- title `{tv.title}` in {len(tv.sources)} CV(s)"
                    + (f": {', '.join(tv.sources)}" if len(tv.sources) <= 3 else "")
                )
        if len(r.employer_variants) > 1:
            out.append(f"- employer written as: {'; '.join(r.employer_variants)}")
        ends = end_variants.get(r.key, Counter())
        if len(ends) > 1:
            out.append(f"- end date varies: {dict(ends)}")
        out.append("")

    out += [
        "## 2. Never-claim terms found in your own CVs",
        "",
        "These CVs already contain a term on the never_claim list. Decide whether the "
        "fact is true (then remove the never_claim item) or whether the CV overstated it "
        "(then delete the fact).",
        "",
    ]
    any_hit = False
    all_text = (
        [(f.id, f.text, f.sources) for f in bank.facts]
        + [(c.id, c.text, c.sources) for c in bank.credentials]
        + [(f"tool:{t.name}", t.name, t.sources) for t in bank.allowed_tools]
    )
    for fid, text, sources in all_text:
        hits = never_claim_hits(text, bank.never_claim)
        if hits:
            any_hit = True
            out.append(f"- `{fid}` ({', '.join(hits)}), in {len(sources)} CV(s): {text[:160]}")
    if not any_hit:
        out.append("- None found.")
    out.append("")

    by_text: dict[str, list[Fact]] = defaultdict(list)
    for f in bank.facts:
        if f.kind == "experience":
            by_text[normalise(f.text)].append(f)
    moved = [fs for fs in by_text.values() if len({f.start for f in fs}) > 1]
    out += [
        "## 2b. Same bullet attributed to different roles",
        "",
        "Across CVs, these bullets sit under more than one role. Keep each under the "
        "role where it actually happened and delete the other copy.",
        "",
    ]
    for fs in moved:
        out.append(f"- {fs[0].text[:150]}")
        out += [f"  - `{f.id}` under {f.role} ({f.start}), {f.source_count} CV(s)" for f in fs]
    if not moved:
        out.append("- None.")
    out.append("")

    years = Counter()
    for f in bank.facts:
        for m in re.finditer(r"\b(\d+)\+?\s+years", f.text):
            years[m.group(0)] += f.source_count
    out += [
        "## 3. Years-of-experience claims",
        "",
        "Different CVs state different totals. Pick one that is true today.",
        "",
    ]
    out += [f'- "{y}" ({c} CV mentions)' for y, c in years.most_common()] or ["- None found."]
    out.append("")

    out += [
        "## 4. Tools in only one or two CVs",
        "",
        "Tools appearing rarely may have been added for one application. Remove any you "
        "could not discuss in depth.",
        "",
    ]
    rare = [t for t in bank.allowed_tools if t.source_count <= 2]
    out += [f"- {t.name}: {', '.join(t.sources)}" for t in rare] or ["- None."]
    out.append("")

    out += ["## 5. Credentials (all variants)", ""]
    for c in sorted(bank.credentials, key=lambda c: (c.kind, -c.source_count)):
        out.append(f"- [{c.kind}] ({c.source_count} CVs) {c.text}")
    out.append("")

    single = [f for f in bank.facts if f.source_count == 1]
    out += [
        "## 6. Facts found in only one CV",
        "",
        f"{len(single)} facts appear in a single CV. These are the most likely to be "
        "tailored wording. Skim them for anything overstated.",
        "",
    ]
    for f in sorted(single, key=lambda f: (f.kind, f.employer or "", f.id)):
        where = f.employer or f.heading or f.kind
        out.append(f"- `{f.id}` [{where}] {f.text[:180]}  _(from {f.sources[0]})_")
    out.append("")
    return "\n".join(out)


def write_bank(bank: EvidenceBank, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bank.model_dump(mode="json", exclude_none=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Evidence bank: the only facts the agent may use. Review and edit by hand.\n")
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=100)


def load_bank(path: Path | str) -> EvidenceBank:
    with open(path, encoding="utf-8") as fh:
        return EvidenceBank.model_validate(yaml.safe_load(fh))
