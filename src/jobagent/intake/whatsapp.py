"""Turn text copied or exported from WhatsApp channels into individual job posts.

No WhatsApp access of any kind: Carol saves text into inbox/*.txt herself (or pastes it into
`jobagent add`). This module only reads that text.

Supported shapes:
- WhatsApp chat exports: "24/11/2026, 10:15 - Channel: text" or
  "[24/11/2026, 10:15:03] Channel: text"
- Several copied posts separated by a line of dashes, equals, underscores or asterisks
- Posts separated by two or more blank lines
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jobagent.intake.scam import EMAIL, PHONE, URL

TIMESTAMP = re.compile(
    r"^\[?(?P<d>\d{1,2})[/.](?P<m>\d{1,2})[/.](?P<y>\d{2,4}),?\s+"
    r"(?P<H>\d{1,2}):(?P<M>\d{2})(?::\d{2})?(?:\s?(?P<ap>[AaPp][Mm]))?\]?"
    r"\s*(?:-\s*)?(?:(?P<who>[^:\n]{1,80}):\s)?",
    re.M,
)
SEPARATOR = re.compile(r"^\s*(?:[-=_*~•]\s*){3,}\s*$", re.M)
SYSTEM_LINES = re.compile(
    r"end-to-end encrypted|<media omitted>|this message was deleted|you deleted this message|"
    r"created (?:this )?(?:group|channel)|changed the (?:subject|group)|joined using this",
    re.I,
)
JOB_WORDS = re.compile(
    r"\b(hiring|vacanc(?:y|ies)|job|position|role|apply|recruit\w*|opportunit(?:y|ies)|"
    r"we(?:'re| are) looking|career|internship|consultan(?:t|cy)|deadline|cv|resume|"
    r"full[- ]?time|part[- ]?time|hybrid|remote|on[- ]?site|salary)\b",
    re.I,
)
LABELLED = {
    "title": re.compile(
        r"^\W*(?:job\s*title|position|role|vacancy|post|job)\s*[:\-–]\s*(?P<v>.+)$", re.I | re.M
    ),
    "company": re.compile(
        r"^\W*(?:company|organi[sz]ation|employer|client|hiring\s+company)\s*[:\-–]\s*(?P<v>.+)$",
        re.I | re.M,
    ),
    "location": re.compile(
        r"^\W*(?:location|based\s+in|duty\s+station|work\s+location|workplace)\s*[:\-–]\s*(?P<v>.+)$",
        re.I | re.M,
    ),
}
IS_HIRING = re.compile(
    r"^\W*(?P<company>[A-Z][\w&.,' -]{1,60}?)\s+(?:is|are)\s+(?:hiring|recruiting|looking\s+for)"
    r"(?:\s*(?:an?|the)?\s*[:\-–]?\s*(?P<title>[^\n.!]{3,80}))?",
    re.M,
)
TITLE_AT_COMPANY = re.compile(
    r"^\W*(?P<title>[^\n@]{3,80}?)\s+(?:at|@)\s+(?P<company>[^\n,|]{2,60})$", re.M
)
HIRING_TITLE = re.compile(
    r"^\W*(?:now\s+)?hiring\W+(?:an?\s+)?(?P<title>[^\n]{3,80})$", re.I | re.M
)


def strip_formatting(text: str) -> str:
    """Remove WhatsApp *bold*, _italic_, ~strike~ and ```mono``` marks and most emoji."""
    text = re.sub(r"```", "", text)
    text = re.sub(r"(?<!\w)[*_~](\S(?:.*?\S)?)[*_~](?!\w)", r"\1", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", "", text)
    return text


@dataclass
class WhatsAppPost:
    text: str
    posted_at: datetime | None = None
    sender: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    links: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        norm = re.sub(r"\W+", " ", self.text.lower()).strip()
        return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _ts(m: re.Match[str]) -> datetime | None:
    d, mo, y = int(m["d"]), int(m["m"]), int(m["y"])
    y = y + 2000 if y < 100 else y
    hour = (
        int(m["H"]) % 12 + (12 if (m["ap"] or "").lower() == "pm" else 0)
        if m["ap"]
        else int(m["H"])
    )
    try:
        # WhatsApp in Kenya exports day/month; if that is impossible, try month/day.
        return datetime(y, mo, d, hour, int(m["M"]), tzinfo=UTC)
    except ValueError:
        try:
            return datetime(y, d, mo, hour, int(m["M"]), tzinfo=UTC)
        except ValueError:
            return None


def split_posts(text: str) -> list[WhatsAppPost]:
    """Split a saved file into posts that look like job adverts."""
    text = text.replace("\r\n", "\n").replace(" ", " ")
    chunks: list[tuple[str, datetime | None, str | None]] = []
    stamps = list(TIMESTAMP.finditer(text))
    if stamps:
        for i, m in enumerate(stamps):
            end = stamps[i + 1].start() if i + 1 < len(stamps) else len(text)
            chunks.append((text[m.end() : end], _ts(m), (m["who"] or "").strip() or None))
    else:
        for part in SEPARATOR.split(text):
            for sub in re.split(r"\n\s*\n\s*\n+", part):
                chunks.append((sub, None, None))

    posts: list[WhatsAppPost] = []
    seen: set[str] = set()
    for body, ts, who in chunks:
        lines = [ln for ln in body.strip().split("\n") if not SYSTEM_LINES.search(ln)]
        body = "\n".join(lines).strip()
        if len(body) < 40 or not (JOB_WORDS.search(body) or URL.search(body)):
            continue
        post = parse_post(body, ts, who)
        if post.content_hash in seen:
            continue
        seen.add(post.content_hash)
        posts.append(post)
    return posts


def _clean(v: str | None) -> str | None:
    if not v:
        return None
    v = re.sub(r"\s+", " ", strip_formatting(v)).strip(" .:-–|*_")
    return v or None


def parse_post(
    text: str, posted_at: datetime | None = None, sender: str | None = None
) -> WhatsAppPost:
    plain = strip_formatting(text)
    found = {k: (m["v"] if (m := rx.search(plain)) else None) for k, rx in LABELLED.items()}
    title, company, location = found["title"], found["company"], found["location"]
    if not (title and company) and (m := IS_HIRING.search(plain)):
        company = company or m["company"]
        title = title or m["title"]
    if not (title and company):
        first_lines = "\n".join(ln for ln in plain.split("\n")[:4] if ln.strip())
        if m := TITLE_AT_COMPANY.search(first_lines):
            title, company = title or m["title"], company or m["company"]
    if not title and (m := HIRING_TITLE.search(plain)):
        title = m["title"]
    if not title:
        title = next(
            (ln for ln in plain.split("\n") if ln.strip() and not URL.search(ln)), "Untitled post"
        )
    links = [u.rstrip(".,;:!)*_") for u in URL.findall(text)]
    links = [u if u.lower().startswith("http") else "https://" + u for u in links]
    return WhatsAppPost(
        text=text.strip(),
        posted_at=posted_at,
        sender=sender,
        title=(_clean(title) or "Untitled post")[:120],
        company=_clean(company),
        location=_clean(location),
        links=list(dict.fromkeys(links)),
        emails=list(dict.fromkeys(EMAIL.findall(text))),
        phones=list(dict.fromkeys(PHONE.findall(text))),
    )
