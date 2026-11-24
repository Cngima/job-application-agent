"""Discovery and community adapters.

Text from these sources is an aggregator's or a stranger's, never the employer's, so jobs are
marked `text_origin` = aggregator/community. Eligibility can only be proven after the resolver
finds the employer's own posting. Location labels go to `aggregator_location` and are never
used to decide eligibility.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from email.utils import parsedate_to_datetime

from jobagent.sourcing.ats import _ts
from jobagent.sourcing.base import Source
from jobagent.sourcing.models import Job
from jobagent.sourcing.text import div_by_class, html_to_text

log = logging.getLogger(__name__)


class RemoteOKSource(Source):
    """https://remoteok.com/api. Terms: link back to the Remote OK URL and name Remote OK."""

    API = "https://remoteok.com/api"

    def fetch(self) -> Iterator[Job]:
        data = self._get_json(self.API)
        for j in data[1:]:  # element 0 is the legal notice
            title = (j.get("position") or "").strip()
            if not title or not self._keep(title):
                continue
            yield Job(
                source=self.cfg.name,
                source_kind=self.cfg.kind,
                title=title,
                company=(j.get("company") or "").strip(),
                url=j["url"],
                apply_url=j.get("apply_url") or None,
                external_id=str(j.get("id")),
                description=html_to_text(j.get("description")),
                text_origin="aggregator",
                aggregator_location=j.get("location") or None,
                salary_min=j.get("salary_min") or None,
                salary_max=j.get("salary_max") or None,
                salary_currency="USD" if j.get("salary_min") else None,
                salary_period="year" if j.get("salary_min") else None,
                posted_at=_ts(j.get("date") or j.get("epoch")),
                tags=j.get("tags") or [],
                attribution=f"Source: Remote OK ({j['url']})",
            )


class HimalayasSource(Source):
    """https://himalayas.app/jobs/api/search. Searched per query with a country filter."""

    API = "https://himalayas.app/jobs/api/search"

    def fetch(self) -> Iterator[Job]:
        p = self.cfg.params
        seen: set[str] = set()
        for q in p.get("queries", []):
            for page in range(1, int(p.get("max_pages", 1)) + 1):
                params = {"q": q, "sort": "recent", "page": page}
                if p.get("country"):
                    params["country"] = p["country"]
                data = self._get_json(self.API, params=params)
                jobs = data.get("jobs", [])
                if not jobs:
                    break
                for j in jobs:
                    if j["guid"] in seen:
                        continue
                    seen.add(j["guid"])
                    if not self._keep(j["title"]):
                        continue
                    period = (j.get("salaryPeriod") or "").lower() or None
                    yield Job(
                        source=self.cfg.name,
                        source_kind=self.cfg.kind,
                        title=j["title"].strip(),
                        company=j["companyName"].strip(),
                        url=j["guid"],
                        apply_url=j.get("applicationLink"),
                        external_id=j["guid"],
                        description=html_to_text(j.get("description")),
                        text_origin="aggregator",
                        aggregator_location=", ".join(j.get("locationRestrictions") or [])
                        or "Worldwide (Himalayas label)",
                        employment_type=j.get("employmentType"),
                        salary_min=j.get("minSalary"),
                        salary_max=j.get("maxSalary"),
                        salary_currency=j.get("currency"),
                        salary_period={"annual": "year", "yearly": "year"}.get(period, period),
                        posted_at=_ts(j.get("pubDate")),
                        tags=j.get("categories") or [],
                        attribution=f"Source: Himalayas ({j['guid']})",
                    )


class JobicySource(Source):
    """https://jobicy.com/api/v2/remote-jobs (public, last 7 days, 1 request/second/IP).
    Terms: credit Jobicy and link to the source."""

    API = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self) -> Iterator[Job]:
        p = self.cfg.params
        seen: set[int] = set()
        for tag in p.get("tags", []):
            params = {"count": int(p.get("count", 50)), "tag": tag}
            if p.get("geo"):
                params["geo"] = p["geo"]
            for j in self._get_json(self.API, params=params).get("jobs", []):
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                if not self._keep(j["jobTitle"]):
                    continue
                geo = j.get("jobGeo")
                yield Job(
                    source=self.cfg.name,
                    source_kind=self.cfg.kind,
                    title=html_to_text(j["jobTitle"]),
                    company=html_to_text(j["companyName"]),
                    url=j["url"],
                    external_id=str(j["id"]),
                    description=html_to_text(j.get("jobDescription")),
                    text_origin="aggregator",
                    aggregator_location=", ".join(geo) if isinstance(geo, list) else geo,
                    employment_type=", ".join(j.get("jobType") or []) or None,
                    salary_min=j.get("salaryMin") or j.get("annualSalaryMin"),
                    salary_max=j.get("salaryMax") or j.get("annualSalaryMax"),
                    salary_currency=j.get("salaryCurrency"),
                    salary_period=j.get("salaryPeriod")
                    or ("year" if j.get("annualSalaryMin") else None),
                    posted_at=_ts(j.get("pubDate")),
                    attribution=f"Source: Jobicy ({j['url']})",
                )


class MyJobMagSource(Source):
    """MyJobMag Kenya's public RSS feed. Most postings are written by the employer, but the
    page is MyJobMag's, so the text is treated as aggregator text."""

    FEED = "https://www.myjobmag.co.ke/jobsxml.xml"

    def fetch(self) -> Iterator[Job]:
        r = self.client.get(
            self.cfg.params.get("feed", self.FEED), respect_robots=self.respect_robots
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)  # bytes: the feed declares ISO-8859-1 itself
        for item in root.iter("item"):
            raw_title = (item.findtext("title") or "").strip()
            title, _, company = raw_title.rpartition(" at ")
            if not title or title.lower().startswith("jobs"):
                continue  # "Jobs at X" bundles several roles; not a single posting
            if not self._keep(title):
                continue
            link = (item.findtext("link") or "").strip()
            gate = self._detail_gate(link)
            if gate == "skip":
                continue
            if gate == "stop":
                break
            page = self._get_text(link)
            body = div_by_class(page, "job-details") or div_by_class(page, "job-description")
            pub = item.findtext("pubDate")
            yield Job(
                source=self.cfg.name,
                source_kind=self.cfg.kind,
                title=title,
                company=company,
                url=link,
                description=html_to_text(body),
                text_origin="aggregator",
                aggregator_location="Kenya (MyJobMag Kenya)",
                posted_at=parsedate_to_datetime(pub) if pub else None,
                attribution=f"Source: MyJobMag ({link})",
            )


# --------------------------------------------------------------------------- Reddit

SCAM_PATTERNS = re.compile(
    r"telegram|whats\s?app|signal app|\bwire (me|us)\b|registration fee|training fee|"
    r"pay (a|the|for)\b|upfront (fee|payment)|deposit|crypto|bitcoin|usdt|gift card|"
    r"cash ?app|western union",
    re.I,
)


class RedditSource(Source):
    """Official Reddit Data API with a personal 'script' app (credentials in .env).

    [Hiring] posts only. Never routed to APPLY: they are unstructured and scam-prone. Posts
    that ask for payment or move to Telegram/WhatsApp are flagged scam_risk.
    """

    TOKEN = "https://www.reddit.com/api/v1/access_token"
    API = "https://oauth.reddit.com/r/{sub}/new"

    def fetch(self) -> Iterator[Job]:
        env = {
            k: os.getenv(f"REDDIT_{k}")
            for k in ("CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PASSWORD")
        }
        if not all(env.values()):
            self.stats.errors.append("skipped: Reddit credentials not set in .env")
            return
        tok = self.client.request(
            "POST",
            self.TOKEN,
            respect_robots=self.respect_robots,
            auth=(env["CLIENT_ID"], env["CLIENT_SECRET"]),
            data={
                "grant_type": "password",
                "username": env["USERNAME"],
                "password": env["PASSWORD"],
            },
        )
        if tok.status_code != 200 or "access_token" not in tok.json():
            self.stats.errors.append(f"skipped: Reddit token request failed ({tok.status_code})")
            return
        headers = {"Authorization": f"bearer {tok.json()['access_token']}"}
        for sub in self.cfg.params.get("subreddits", []):
            data = self._get_json(self.API.format(sub=sub), params={"limit": 50}, headers=headers)
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                raw = post.get("title", "")
                if not re.search(r"\[hiring\]", raw, re.I):
                    continue
                title = re.sub(r"\[[^\]]*\]\s*", "", raw).strip()
                if not self._keep(title):
                    continue
                text = post.get("selftext", "")
                url = f"https://www.reddit.com{post.get('permalink', '')}"
                yield Job(
                    source=self.cfg.name,
                    source_kind="community",
                    title=title,
                    company=f"Reddit u/{post.get('author', 'unknown')}",
                    url=url,
                    external_id=post.get("id"),
                    description=text,
                    text_origin="community",
                    aggregator_location=f"r/{sub}",
                    posted_at=_ts(post.get("created_utc")),
                    flags=["scam_risk"] if SCAM_PATTERNS.search(f"{raw}\n{text}") else [],
                )


DISCOVERY_ADAPTERS: dict[str, type[Source]] = {
    "remoteok": RemoteOKSource,
    "himalayas": HimalayasSource,
    "jobicy": JobicySource,
    "myjobmag": MyJobMagSource,
    "reddit": RedditSource,
}
