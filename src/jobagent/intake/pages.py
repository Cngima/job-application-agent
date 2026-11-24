"""Follow an apply link from a WhatsApp or manual post to the original advert.

Rules:
- GET only. Never submits a form, never logs in.
- Redirects are followed by hand (up to 5 hops) so robots.txt is checked on every host, not
  only the first one (link shorteners often hop to another site).
- Hosts that must never be fetched (LinkedIn, Indeed, WhatsApp, Telegram, social media) stop
  the chain immediately.
- Supported ATS postings are read through their official APIs.
- Other pages: schema.org JobPosting data if present, else the page text. The text counts as
  the employer's own only when the page is on an ATS or on the hiring organisation's domain.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from jobagent.sourcing.ats import _ts, match_ats_url
from jobagent.sourcing.http import HostUnavailable, PoliteClient, RobotsDisallowed
from jobagent.sourcing.models import Job, SourceKind
from jobagent.sourcing.text import html_to_text

log = logging.getLogger(__name__)

NEVER_FETCH = re.compile(
    r"(^|\.)(linkedin\.com|lnkd\.in|indeed\.[a-z.]+|wa\.me|whatsapp\.com|t\.me|telegram\.(me|org)|"
    r"facebook\.com|fb\.me|instagram\.com|x\.com|twitter\.com|tiktok\.com)$",
    re.I,
)
# Applicant tracking systems: a posting hosted here is the employer's own advert.
ATS_HOSTS = re.compile(
    r"(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|breezy\.hr|zohorecruit\.[a-z]+|"
    r"smartrecruiters\.com|bamboohr\.com|recruitee\.com|teamtailor\.com|personio\.(de|com)|"
    r"jobvite\.com|icims\.com|myworkdayjobs\.com|applytojob\.com|rippling(-ats)?\.com|"
    r"freshteam\.com|pinpointhq\.com|jobs\.gem\.com|homerun\.co|manatal\.com|successfactors\.[a-z]+|"
    r"oraclecloud\.com|taleo\.net|ultipro\.com|paylocity\.com)$",
    re.I,
)
MAX_HOPS = 5


@dataclass
class Advert:
    url: str  # final URL after redirects
    title: str | None
    company: str | None
    location: str | None
    description: str
    is_employer: bool
    posted_at: object = None
    job: Job | None = None  # set when the link was a supported ATS posting


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().split(":")[0]


def _site(host: str) -> str:
    """Registered-domain approximation: last two labels (three for .co.ke style)."""
    parts = host.split(".")
    if (
        len(parts) >= 3
        and len(parts[-1]) == 2
        and parts[-2] in ("co", "or", "ac", "go", "com", "org")
    ):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def never_fetch(url: str) -> bool:
    return bool(NEVER_FETCH.search(_host(url))) or not url.lower().startswith("http")


def _job_posting(page: str) -> dict | None:
    for m in re.finditer(
        r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I
    ):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                kind = item.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    return item
                stack.extend(item.get("@graph", []) if isinstance(item.get("@graph"), list) else [])
    return None


def _jp_location(jp: dict) -> str | None:
    parts: list[str] = []
    if jp.get("jobLocationType"):
        parts.append(str(jp["jobLocationType"]))
    for key in ("applicantLocationRequirements", "jobLocation"):
        vals = jp.get(key)
        for v in vals if isinstance(vals, list) else [vals]:
            if isinstance(v, dict):
                addr = v.get("address") if isinstance(v.get("address"), dict) else v
                txt = ", ".join(
                    str(addr.get(k))
                    for k in ("addressLocality", "addressRegion", "addressCountry", "name")
                    if addr.get(k) and isinstance(addr.get(k), str)
                )
                if txt:
                    parts.append(txt)
    return "; ".join(dict.fromkeys(parts)) or None


def fetch_advert(client: PoliteClient, url: str, kind: SourceKind = "community") -> Advert | None:
    """Return the advert behind `url`, or None if it cannot (or must not) be read."""
    for _ in range(MAX_HOPS):
        if never_fetch(url):
            log.info("not fetching %s (blocked host)", url)
            return None
        if hit := match_ats_url(url):
            cls, slug, ref = hit
            try:
                job = cls.fetch_one(client, slug, ref, slug, kind)
            except Exception as e:  # an unreadable ATS posting must not stop the batch
                log.info("ATS fetch failed for %s: %s", url, e)
                return None
            if job is None:
                return None
            return Advert(
                url=job.employer_url or url,
                title=job.title,
                company=None,
                location=job.location_text,
                description=job.description,
                is_employer=True,
                posted_at=job.posted_at,
                job=job,
            )
        try:
            r = client.get(url, follow_redirects=False)
        except (RobotsDisallowed, HostUnavailable) as e:
            log.info("not fetching %s: %s", url, e)
            return None
        except Exception as e:
            log.info("fetch failed for %s: %s", url, e)
            return None
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return None
        page = r.text
        host = _host(url)
        if jp := _job_posting(page):
            org = jp.get("hiringOrganization") or {}
            org_urls = [org.get("sameAs"), org.get("url")] if isinstance(org, dict) else []
            on_org_site = any(
                isinstance(u, str) and _site(_host(u)) == _site(host) for u in org_urls
            )
            return Advert(
                url=url,
                title=html_to_text(jp.get("title")) or None,
                company=org.get("name") if isinstance(org, dict) else None,
                location=_jp_location(jp),
                description=html_to_text(jp.get("description")),
                is_employer=bool(ATS_HOSTS.search(host)) or on_org_site,
                posted_at=_ts(jp.get("datePosted")),
            )
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        body = re.sub(r"(?is)<(nav|header|footer|aside|form)\b.*?</\1>", " ", page)
        return Advert(
            url=url,
            title=html_to_text(title.group(1)) if title else None,
            company=None,
            location=None,
            description=html_to_text(body)[:20000],
            is_employer=bool(ATS_HOSTS.search(host)),
        )
    log.info("too many redirects for %s", url)
    return None
