"""Parse one CV (as a list of lines) into structured parts.

Rule-based on purpose: the CVs share a house layout, so plain parsing is cheaper, faster and
fully traceable compared with asking a model to extract facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_RE = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_POINT = rf"(?:{_MONTH_RE}\s+\d{{4}}|\d{{4}})"
DATE_RANGE = re.compile(
    rf"(?P<start>{_POINT})\s*[–\-]\s*(?P<end>Present|Current|{_POINT})\s*$", re.IGNORECASE
)

SECTIONS: dict[str, str] = {
    "PROFESSIONAL PROFILE": "profile",
    "CORE COMPETENCIES": "competencies",
    "SKILLS SNAPSHOT": "skills",
    "PROFESSIONAL EXPERIENCE": "experience",
    "RELEVANT PROFESSIONAL EXPERIENCE": "experience",
    "ADDITIONAL WORK EXPERIENCE": "experience",
    "VOLUNTEERING EXPERIENCE": "experience",
    "SELECTED ACHIEVEMENTS": "achievements",
    "KEY PROJECTS HIGHLIGHTS": "projects",
    "EDUCATION": "cred:education",
    "LICENSES & CERTIFICATIONS": "cred:certification",
    "PROFESSIONAL CERTIFICATIONS": "cred:certification",
    "PROFESSIONAL MEMBERSHIP": "cred:membership",
    "LEADERSHIP, MEMBERSHIPS & COMMUNITY": "cred:membership",
    "LANGUAGES": "cred:language",
    "ADDITIONAL INFORMATION": "cred:other",
    "REFEREES": "ignore",
    "REFERENCES": "ignore",
}

SKILL_LINE = re.compile(
    r"^(?P<label>Technical Skills|Other Skills|Tools|Skills|Soft Skills)\s*:\s*(?P<body>.+)$",
    re.IGNORECASE,
)
LABEL_LINES = {
    "role responsibilities", "key achievements", "duties & responsibilities",
    "responsibilities", "achievements",
}
LOCATION_WORDS = ("kenya", "nairobi", "nakuru", "canada", "ottawa", "united states", "remote")
BULLET_CHARS = "◆•▪●-* \t"


@dataclass
class ParsedRole:
    title: str
    employer: str | None
    start: str | None
    end: str | None
    bullets: list[str] = field(default_factory=list)


@dataclass
class ParsedCV:
    source: str
    name: str = ""
    header_lines: list[str] = field(default_factory=list)
    profile: list[str] = field(default_factory=list)
    competencies: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    roles: list[ParsedRole] = field(default_factory=list)
    achievements: list[tuple[str, str | None, str]] = field(default_factory=list)
    projects: list[tuple[str, str]] = field(default_factory=list)
    credentials: list[tuple[str, str]] = field(default_factory=list)


def to_ym(point: str) -> str | None:
    """'Sept 2019' -> '2019-09', '2014' -> '2014', 'Present' -> 'present'."""
    point = point.strip().rstrip(".")
    if point.lower() in ("present", "current"):
        return "present"
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{4})", point)
    if m:
        month = MONTHS.get(m.group(1).lower())
        return f"{m.group(2)}-{month:02d}" if month else m.group(2)
    m = re.match(r"\d{4}", point)
    return m.group(0) if m else None


def strip_bullet(text: str) -> str:
    return text.lstrip(BULLET_CHARS).strip()


def clean_employer(raw: str) -> str:
    """'Assent Inc., Ottawa, Canada (Remote)' -> 'Assent Inc.'; keeps group names."""
    emp = raw.split("•")[0].strip()
    emp = re.sub(r"\s*\((?:Remote|Kenya Office|via [^)]*)\)\s*$", "", emp, flags=re.I)
    parts = [p.strip() for p in emp.split(",")]
    while len(parts) > 1 and any(w in parts[-1].lower() for w in LOCATION_WORDS):
        parts.pop()
    return ", ".join(parts).strip(" |")


def parse_role_header(line: str) -> ParsedRole | None:
    m = DATE_RANGE.search(line)
    if not m or len(line) > 220:
        return None
    head = line[: m.start()].strip().rstrip("|").strip()
    head = re.sub(r"^Earlier role:\s*", "", head, flags=re.I)
    if not head:
        return None
    parts = [p.strip() for p in re.split(r"\s*\|\s*", head) if p.strip()]
    title, employer = parts[0], None
    if len(parts) > 1:
        employer = clean_employer(parts[1])
    else:
        segs = [s.strip() for s in head.split(",")]
        # "Title, Employer, City, Country" one-liners: split only when a location is present.
        is_loc = [any(w in s.lower() for w in LOCATION_WORDS) for s in segs]
        loc_idx = next((i for i, flag in enumerate(is_loc) if i > 0 and flag), None)
        if loc_idx is not None and loc_idx >= 2:
            title, employer = ", ".join(segs[: loc_idx - 1]), segs[loc_idx - 1]
    title = re.sub(r"\s{2,}", " ", title)
    return ParsedRole(title, employer, to_ym(m.group("start")), to_ym(m.group("end")))


def _is_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return bool(letters) and line.isupper() and len(line) < 60


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text) if s.strip()]


def split_tools(body: str) -> list[str]:
    """Split a 'Technical Skills:' line into tool names."""
    tools: list[str] = []
    for chunk in re.split(r"\s*[|;]\s*", body):
        chunk = re.sub(r"\([^)]*\)", "", chunk)  # 'Claude (AI agents & skills)' -> 'Claude'
        left, *rest = re.split(r"\s+[–-]\s+", chunk)
        # 'Box – Cloud Content Management' -> 'Box', but 'MS – Office Suite' -> 'MS Office Suite'
        chunk = f"{left} {rest[0]}" if rest and len(left) <= 2 else left
        for part in re.split(r"\s*/\s*", chunk):
            # 'Asana & ClickUp' is two tools; 'GitHub Issues & Projects' is one.
            pieces = re.split(r"\s+&\s+", part)
            if any(p.strip() in _GENERIC_SUFFIXES for p in pieces[1:]):
                pieces = [part]
            for piece in pieces:
                piece = piece.strip(" .,")
                if piece:
                    tools.append(piece)
    return tools


_GENERIC_SUFFIXES = {"Projects", "Issues", "Reporting", "Dashboards", "Suite"}


def parse_cv(lines: list[str], source: str) -> ParsedCV:
    cv = ParsedCV(source=source)
    if lines:
        cv.name = lines[0].strip().title()
    section = "header"
    current_role: ParsedRole | None = None
    awaiting_employer = False
    current_ach: list | None = None  # [heading, employer, [texts]]
    current_proj: list | None = None

    def flush_ach() -> None:
        nonlocal current_ach
        if current_ach and current_ach[2]:
            cv.achievements.append((current_ach[0], current_ach[1], " ".join(current_ach[2])))
        current_ach = None

    for raw in lines[1:]:
        line = raw.strip()
        if _is_heading(line):
            sec = SECTIONS.get(line.strip())
            if sec:
                flush_ach()
                section, current_role, current_proj = sec, None, None
                continue
            if section == "header":
                continue  # headline like "CX OPERATIONS LEADER & ..."
        skill = SKILL_LINE.match(line)
        if skill:
            if skill.group("label").lower().startswith("soft"):
                cv.competencies.extend(split_tools(skill.group("body")))
            else:
                cv.tools.extend(split_tools(skill.group("body")))
            continue

        if section == "header":
            cv.header_lines.append(line)
        elif section == "profile":
            cv.profile.extend(_split_sentences(line))
        elif section == "competencies":
            for item in re.split(r"\s*/\s*", strip_bullet(line)):
                if item.strip():
                    cv.competencies.append(item.strip())
        elif section == "experience":
            role = parse_role_header(line)
            if role:
                cv.roles.append(role)
                current_role = role
                awaiting_employer = role.employer is None
                continue
            if current_role is None:
                continue
            text = strip_bullet(line)
            if text.lower().rstrip(":").strip() in LABEL_LINES or (
                text.endswith(":") and len(text) < 40
            ):
                continue  # 'Key Achievement:', 'Core Responsibilities:' and similar labels
            if awaiting_employer:
                awaiting_employer = False
                if len(text) < 90 and not text.endswith("."):
                    current_role.employer = clean_employer(text)
                    continue
            current_role.bullets.append(text)
        elif section == "achievements":
            if line.startswith("◆") and "|" in line:
                flush_ach()
                heading, _, rest = strip_bullet(line).partition("|")
                employer = re.sub(r",?\s*\d{4}(\s*[–-]\s*\d{4})?\s*$", "", rest).strip() or None
                current_ach = [heading.strip(), employer, []]
            elif current_ach is not None:
                current_ach[2].append(strip_bullet(line))
        elif section == "projects":
            m = re.match(r"^(.+?)\s*\((\d{4})\)\s*$", line)
            if m:
                current_proj = [m.group(1).strip(), m.group(2)]
            elif current_proj is not None:
                cv.projects.append((f"{current_proj[0]} ({current_proj[1]})", strip_bullet(line)))
        elif section.startswith("cred:"):
            kind = section.split(":", 1)[1]
            text = strip_bullet(line)
            if text.lower().startswith("languages:"):
                kind = "language"
            cv.credentials.append((kind, text))
    flush_ach()
    return cv
