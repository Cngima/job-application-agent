"""Adapters for public applicant tracking system (ATS) job boards.

These read the employer's own postings, so their text can prove eligibility. Each adapter also
knows how to recognise one of its posting URLs and fetch that single posting, which the
discovery resolver and `jobagent add` use.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

from jobagent.sourcing.base import Source
from jobagent.sourcing.http import PoliteClient
from jobagent.sourcing.models import Job, SourceKind
from jobagent.sourcing.text import div_by_class, html_to_text

log = logging.getLogger(__name__)


def _ts(value) -> datetime | None:
    """Parse ISO strings or epoch seconds/milliseconds into aware datetimes."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class ATSSource(Source):
    ats: str = ""
    URL_RE: re.Pattern[str]

    @property
    def company(self) -> str:
        return self.cfg.params.get("company", self.cfg.name)

    @classmethod
    def match_url(cls, url: str) -> tuple[str, str] | None:
        """Return (slug, posting_ref) if `url` is one of this ATS's posting pages."""
        m = cls.URL_RE.search(url)
        return (m.group("slug"), m.group("ref")) if m else None

    @classmethod
    def fetch_one(
        cls, client: PoliteClient, slug: str, ref: str, company: str, kind: SourceKind
    ) -> Job | None:
        raise NotImplementedError


# --------------------------------------------------------------------------- Greenhouse


class GreenhouseSource(ATSSource):
    ats = "greenhouse"
    API = "https://boards-api.greenhouse.io/v1/boards/{slug}"
    URL_RE = re.compile(r"(?:job-)?boards\.greenhouse\.io/(?P<slug>[\w-]+)/jobs/(?P<ref>\d+)", re.I)

    @classmethod
    def _job(cls, j: dict, slug: str, company: str, kind: SourceKind, name: str) -> Job:
        return Job(
            source=name,
            source_kind=kind,
            title=j["title"].strip(),
            company=company,
            url=j["absolute_url"],
            employer_url=j["absolute_url"],
            apply_url=j["absolute_url"],
            external_id=str(j["id"]),
            description=html_to_text(j.get("content")),
            location_text=(j.get("location") or {}).get("name"),
            posted_at=_ts(j.get("first_published") or j.get("updated_at")),
        )

    def fetch(self) -> Iterator[Job]:
        data = self._get_json(
            self.API.format(slug=self.cfg.slug) + "/jobs", params={"content": "true"}
        )
        for j in data.get("jobs", []):
            if self._keep(j["title"]):
                yield self._job(j, self.cfg.slug, self.company, self.cfg.kind, self.cfg.name)

    @classmethod
    def fetch_one(cls, client, slug, ref, company, kind):
        j = client.get_json(cls.API.format(slug=slug) + f"/jobs/{ref}")
        return cls._job(j, slug, company, kind, f"greenhouse:{slug}")


# --------------------------------------------------------------------------- Lever


class LeverSource(ATSSource):
    ats = "lever"
    API = "https://api.lever.co/v0/postings/{slug}"
    URL_RE = re.compile(r"jobs\.lever\.co/(?P<slug>[\w-]+)/(?P<ref>[0-9a-f-]{36})", re.I)

    @classmethod
    def _job(cls, j: dict, company: str, kind: SourceKind, name: str) -> Job:
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", ""))
            parts.append(html_to_text(lst.get("content")))
        parts.append(j.get("additionalPlain") or html_to_text(j.get("additional")))
        sal = j.get("salaryRange") or {}
        return Job(
            source=name,
            source_kind=kind,
            title=j["text"].strip(),
            company=company,
            url=j["hostedUrl"],
            employer_url=j["hostedUrl"],
            apply_url=j.get("applyUrl"),
            external_id=j["id"],
            description="\n\n".join(p for p in parts if p).strip(),
            location_text=", ".join(
                filter(None, [cats.get("location"), *(cats.get("allLocations") or [])][:3])
            )
            or None,
            employment_type=cats.get("commitment"),
            remote=(j.get("workplaceType") == "remote") if j.get("workplaceType") else None,
            salary_min=sal.get("min"),
            salary_max=sal.get("max"),
            salary_currency=sal.get("currency"),
            salary_period={
                "per-year-salary": "year",
                "per-month-salary": "month",
                "per-hour-wage": "hour",
            }.get(sal.get("interval", "")),
            posted_at=_ts(j.get("createdAt")),
        )

    def fetch(self) -> Iterator[Job]:
        for j in self._get_json(self.API.format(slug=self.cfg.slug), params={"mode": "json"}):
            if self._keep(j["text"]):
                yield self._job(j, self.company, self.cfg.kind, self.cfg.name)

    @classmethod
    def fetch_one(cls, client, slug, ref, company, kind):
        j = client.get_json(cls.API.format(slug=slug) + f"/{ref}", params={"mode": "json"})
        return cls._job(j, company, kind, f"lever:{slug}")


# --------------------------------------------------------------------------- Ashby


class AshbySource(ATSSource):
    ats = "ashby"
    API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
    URL_RE = re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[^/?#]+)/(?P<ref>[0-9a-f-]{36})", re.I)

    @classmethod
    def _job(cls, j: dict, company: str, kind: SourceKind, name: str) -> Job:
        locs = [j.get("location")] + [
            s.get("location") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)
        ]
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        return Job(
            source=name,
            source_kind=kind,
            title=j["title"].strip(),
            company=company,
            url=j["jobUrl"],
            employer_url=j["jobUrl"],
            apply_url=j.get("applyUrl"),
            external_id=j["id"],
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            location_text=", ".join(x for x in locs if x) or None,
            employment_type=j.get("employmentType"),
            remote=j.get("isRemote"),
            salary_text=comp,
            posted_at=_ts(j.get("publishedAt")),
        )

    def _board(self, slug: str) -> list[dict]:
        url = self.API.format(slug=quote(slug))
        return self._get_json(url, params={"includeCompensation": "true"}).get("jobs", [])

    def fetch(self) -> Iterator[Job]:
        for j in self._board(self.cfg.slug):
            if j.get("isListed", True) and self._keep(j["title"]):
                yield self._job(j, self.company, self.cfg.kind, self.cfg.name)

    @classmethod
    def fetch_one(cls, client, slug, ref, company, kind):
        # Ashby has no public single-posting endpoint, so read the board and pick the id.
        data = client.get_json(
            cls.API.format(slug=quote(slug)), params={"includeCompensation": "true"}
        )
        j = next((x for x in data.get("jobs", []) if x["id"] == ref), None)
        return cls._job(j, company, kind, f"ashby:{slug}") if j else None


# --------------------------------------------------------------------------- Workable


class WorkableSource(ATSSource):
    """The widget list has no descriptions, so details are fetched per posting after the
    title prefilter. Pavago alone lists over 2,600 jobs."""

    ats = "workable"
    LIST = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
    DETAIL = "https://apply.workable.com/api/v1/accounts/{slug}/jobs/{ref}"
    URL_RE = re.compile(r"apply\.workable\.com/(?P<slug>[\w-]+)/j/(?P<ref>[0-9A-F]{6,})", re.I)

    @classmethod
    def _job(cls, d: dict, slug: str, company: str, kind: SourceKind, name: str) -> Job:
        loc = d.get("location") or {}
        locs = [loc] + [x for x in (d.get("locations") or []) if x != loc]
        loc_text = "; ".join(
            ", ".join(filter(None, [x.get("city"), x.get("region"), x.get("country")]))
            for x in locs
            if isinstance(x, dict)
        )
        url = f"https://apply.workable.com/{slug}/j/{d['shortcode']}/"
        body = "\n\n".join(
            html_to_text(d.get(k)) for k in ("description", "requirements", "benefits") if d.get(k)
        )
        return Job(
            source=name,
            source_kind=kind,
            title=d["title"].strip(),
            company=company,
            url=url,
            employer_url=url,
            apply_url=url + "apply/",
            external_id=d["shortcode"],
            description=body,
            location_text=loc_text or None,
            employment_type=d.get("type"),
            remote=d.get("remote"),
            posted_at=_ts(d.get("published")),
        )

    def fetch(self) -> Iterator[Job]:
        data = self._get_json(self.LIST.format(slug=self.cfg.slug))
        company = data.get("name") or self.company
        for j in data.get("jobs", []):
            if not self._keep(j["title"]):
                continue
            if not self.is_fresh(_ts(j.get("published_on"))):
                continue
            gate = self._detail_gate(
                f"https://apply.workable.com/{self.cfg.slug}/j/{j['shortcode']}/"
            )
            if gate == "skip":
                continue
            if gate == "stop":
                break
            d = self._get_json(self.DETAIL.format(slug=self.cfg.slug, ref=j["shortcode"]))
            yield self._job(d, self.cfg.slug, company, self.cfg.kind, self.cfg.name)

    @classmethod
    def fetch_one(cls, client, slug, ref, company, kind):
        d = client.get_json(cls.DETAIL.format(slug=slug, ref=ref))
        return cls._job(d, slug, company, kind, f"workable:{slug}")


# --------------------------------------------------------------------------- Breezy


class BreezySource(ATSSource):
    """The /json feed lists postings; the description is on each public posting page."""

    ats = "breezy"
    LIST = "https://{slug}.breezy.hr/json"
    URL_RE = re.compile(r"(?P<slug>[\w-]+)\.breezy\.hr/p/(?P<ref>[\w-]+)", re.I)

    @classmethod
    def _page_text(cls, client: PoliteClient, url: str, respect: bool = True) -> str:
        r = client.get(url, respect_robots=respect)
        r.raise_for_status()
        return html_to_text(div_by_class(r.text, "description"))

    def fetch(self) -> Iterator[Job]:
        for p in self._get_json(self.LIST.format(slug=self.cfg.slug)):
            if not self._keep(p["name"]):
                continue
            if not self.is_fresh(_ts(p.get("published_date"))):
                continue
            gate = self._detail_gate(p["url"])
            if gate == "skip":
                continue
            if gate == "stop":
                break
            sal = p.get("salary")
            yield Job(
                source=self.cfg.name,
                source_kind=self.cfg.kind,
                title=p["name"].strip(),
                company=(p.get("company") or {}).get("name") or self.company,
                url=p["url"],
                employer_url=p["url"],
                apply_url=p["url"],
                external_id=p["id"],
                description=self._page_text(self.client, p["url"], self.respect_robots),
                location_text=(p.get("location") or {}).get("name"),
                employment_type=(p.get("type") or {}).get("name"),
                salary_text=sal if isinstance(sal, str) and sal else None,
                posted_at=_ts(p.get("published_date")),
            )

    @classmethod
    def fetch_one(cls, client, slug, ref, company, kind):
        url = f"https://{slug}.breezy.hr/p/{ref}"
        r = client.get(url)
        r.raise_for_status()
        title = re.search(r"<h1[^>]*>(.*?)</h1>", r.text, re.S)
        return Job(
            source=f"breezy:{slug}",
            source_kind=kind,
            title=html_to_text(title.group(1)) if title else ref,
            company=company,
            url=url,
            employer_url=url,
            apply_url=url,
            external_id=ref,
            description=html_to_text(div_by_class(r.text, "description")),
        )


# --------------------------------------------------------------------------- Zoho Recruit


class _HiddenInputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "input" and a.get("type") == "hidden" and a.get("id"):
            self.values[a["id"]] = a.get("value") or ""


_JS_ESC = {"n": "\n", "t": "\t", "r": "\r"}


def js_unescape(s: str) -> str:
    """Undo JavaScript string escaping (\\x22, \\u00e9, \\/ ...)."""

    def rep(m: re.Match[str]) -> str:
        g = m.group(1)
        if g[0] in "xu" and len(g) > 1:
            return chr(int(g[1:], 16))
        return _JS_ESC.get(g, g)

    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", rep, s, flags=re.S)


class ZohoRecruitSource(ATSSource):
    """Zoho Recruit career portals (robots.txt allows /jobs/). The list page embeds postings
    in a hidden input; each posting page embeds its details in `var jobs = JSON.parse('...')`."""

    ats = "zoho"
    LIST = "https://{slug}.zohorecruit.com/jobs/Careers"
    URL_RE = re.compile(r"(?P<slug>[\w-]+)\.zohorecruit\.com/jobs/Careers/(?P<ref>\d+)", re.I)

    @classmethod
    def parse_list(cls, page: str) -> list[dict]:
        p = _HiddenInputs()
        p.feed(page)
        return json.loads(p.values.get("jobs") or "[]")

    @classmethod
    def parse_detail(cls, page: str) -> dict | None:
        m = re.search(r"var jobs = JSON\.parse\('(.*?)'\);", page, re.S)
        if not m:
            return None
        data = json.loads(js_unescape(m.group(1)))
        return data[0] if data else None

    @classmethod
    def _job(cls, d: dict, slug: str, company: str, kind: SourceKind, name: str) -> Job:
        url = f"https://{slug}.zohorecruit.com/jobs/Careers/{d['id']}"
        loc = ", ".join(filter(None, [d.get("City"), d.get("State"), d.get("Country")]))
        desc = html_to_text(d.get("Job_Description"))
        if d.get("Work_Timezone"):
            desc = f"Working hours: {d['Work_Timezone']}\n\n{desc}"
        return Job(
            source=name,
            source_kind=kind,
            title=(d.get("Posting_Title") or "").strip(),
            company=company,
            url=url,
            employer_url=url,
            apply_url=url,
            external_id=str(d["id"]),
            description=desc,
            location_text=loc or None,
            employment_type=d.get("Job_Type"),
            remote=d.get("Remote_Job"),
            posted_at=_ts(d.get("Date_Opened")),
        )

    def fetch(self) -> Iterator[Job]:
        for j in self.parse_list(self._get_text(self.LIST.format(slug=self.cfg.slug))):
            if not j.get("Publish", True) or not self._keep(j.get("Posting_Title", "")):
                continue
            gate = self._detail_gate(f"{self.LIST.format(slug=self.cfg.slug)}/{j['id']}")
            if gate == "skip":
                continue
            if gate == "stop":
                break
            page = self._get_text(f"{self.LIST.format(slug=self.cfg.slug)}/{j['id']}")
            d = self.parse_detail(page) or j
            yield self._job(d, self.cfg.slug, self.company, self.cfg.kind, self.cfg.name)

    @classmethod
    def fetch_one(cls, client, slug, ref, company, kind):
        r = client.get(f"{cls.LIST.format(slug=slug)}/{ref}")
        r.raise_for_status()
        d = cls.parse_detail(r.text)
        return cls._job(d, slug, company, kind, f"zoho:{slug}") if d else None


ATS_ADAPTERS: dict[str, type[ATSSource]] = {
    c.ats: c
    for c in (
        GreenhouseSource,
        LeverSource,
        AshbySource,
        WorkableSource,
        BreezySource,
        ZohoRecruitSource,
    )
}


def match_ats_url(url: str) -> tuple[type[ATSSource], str, str] | None:
    """Identify an employer ATS posting URL: (adapter class, slug, posting ref)."""
    host = urlsplit(url).netloc.lower()
    for cls in ATS_ADAPTERS.values():
        m = cls.match_url(url)
        if m and (cls.ats in host or (cls.ats == "zoho" and "zohorecruit" in host)):
            return cls, m[0], m[1]
    return None
