"""Stage 2: every source adapter against recorded-shape responses (no network)."""

from __future__ import annotations

import json

import pytest

from jobagent.sourcing.ats import (
    AshbySource,
    BreezySource,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    ZohoRecruitSource,
    match_ats_url,
)
from jobagent.sourcing.base import SourcingContext
from jobagent.sourcing.config import SourceConfig, TitlePrefilter
from jobagent.sourcing.discovery import (
    HimalayasSource,
    JobicySource,
    MyJobMagSource,
    RedditSource,
    RemoteOKSource,
)

PREFILTER = TitlePrefilter(
    include=["project", "automation", "implementation"], exclude=["engineer"]
)


def ctx(cap: int = 60) -> SourcingContext:
    return SourcingContext(title_ok=PREFILTER.passes, max_detail_fetches=cap)


def cfg(
    adapter: str, kind: str = "employer_ats", slug: str | None = "acme", **params
) -> SourceConfig:
    return SourceConfig(
        name=f"Acme {adapter}", adapter=adapter, kind=kind, slug=slug, params=params
    )


# ---------- ATS adapters


def test_greenhouse(make_client) -> None:
    client, router = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/acme/jobs": {
                "jobs": [
                    {
                        "id": 1,
                        "title": "AI Project Manager",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                        "content": "&lt;p&gt;Remote, open to candidates in Kenya.&lt;/p&gt;",
                        "location": {"name": "Remote - EMEA"},
                        "updated_at": "2026-11-01T10:00:00-04:00",
                    },
                    {
                        "id": 2,
                        "title": "Software Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                    },
                ]
            }
        }
    )
    src = GreenhouseSource(cfg("greenhouse"), client, ctx())
    (job,) = list(src.fetch())
    assert job.title == "AI Project Manager" and job.text_origin == "employer"
    assert job.description == "Remote, open to candidates in Kenya."
    assert job.location_text == "Remote - EMEA" and job.posted_at.year == 2026
    assert (src.stats.listed, src.stats.title_filtered) == (2, 1)
    assert router.calls[-1].url.params["content"] == "true"


def test_lever(make_client) -> None:
    client, _ = make_client(
        {
            "api.lever.co/v0/postings/acme": [
                {
                    "id": "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12,
                    "text": "Implementation Manager",
                    "hostedUrl": "https://jobs.lever.co/acme/x",
                    "applyUrl": "https://jobs.lever.co/acme/x/apply",
                    "descriptionPlain": "Own onboarding.",
                    "lists": [{"text": "Requirements", "content": "<li>HubSpot</li>"}],
                    "categories": {"location": "Nairobi", "commitment": "Part-time"},
                    "workplaceType": "remote",
                    "createdAt": 1795000000000,
                    "salaryRange": {
                        "min": 20,
                        "max": 30,
                        "currency": "USD",
                        "interval": "per-hour-wage",
                    },
                }
            ]
        }
    )
    (job,) = list(LeverSource(cfg("lever"), client, ctx()).fetch())
    assert "Requirements" in job.description and "HubSpot" in job.description
    assert (job.remote, job.employment_type, job.salary_period) == (True, "Part-time", "hour")


def test_ashby_skips_unlisted(make_client) -> None:
    client, _ = make_client(
        {
            "api.ashbyhq.com/posting-api/job-board/acme": {
                "jobs": [
                    {
                        "id": "1",
                        "title": "Automation Lead",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/1",
                        "descriptionPlain": "Text",
                        "location": "Kenya",
                        "isRemote": True,
                        "isListed": True,
                    },
                    {"id": "2", "title": "Automation Lead 2", "jobUrl": "u", "isListed": False},
                ]
            }
        }
    )
    jobs = list(AshbySource(cfg("ashby"), client, ctx()).fetch())
    assert [j.external_id for j in jobs] == ["1"]


def test_workable_fetches_details_only_after_prefilter_and_respects_cap(make_client) -> None:
    listing = {
        "name": "Acme Agency",
        "jobs": [
            {"title": "Automation Specialist", "shortcode": "AAA111"},
            {"title": "Accountant", "shortcode": "BBB222"},
            {"title": "Project Coordinator", "shortcode": "CCC333"},
        ],
    }

    def detail(code: str, title: str) -> dict:
        return {
            "shortcode": code,
            "title": title,
            "description": "<p>Remote</p>",
            "requirements": "<p>Zapier</p>",
            "remote": True,
            "location": {"country": "Kenya", "city": "Nairobi"},
        }

    client, router = make_client(
        {
            "apply.workable.com/api/v1/widget/accounts/acme": listing,
            "apply.workable.com/api/v1/accounts/acme/jobs/AAA111": detail(
                "AAA111", "Automation Specialist"
            ),
            "apply.workable.com/api/v1/accounts/acme/jobs/CCC333": detail(
                "CCC333", "Project Coordinator"
            ),
        }
    )
    src = WorkableSource(cfg("workable", kind="agency"), client, ctx(cap=1))
    jobs = list(src.fetch())
    assert [j.external_id for j in jobs] == ["AAA111"]  # cap of 1 detail fetch
    assert jobs[0].company == "Acme Agency" and "Zapier" in jobs[0].description
    assert jobs[0].location_text == "Nairobi, Kenya"
    assert not any("BBB222" in p for p in router.paths())  # filtered title never fetched
    assert any("cap reached" in e for e in src.stats.errors)


def test_breezy_reads_description_from_posting_page(make_client) -> None:
    page = '<div class="banner"></div><div class="description"><p>Open to Africa.</p></div>'
    client, _ = make_client(
        {
            "acme.breezy.hr/json": [
                {
                    "id": "p1",
                    "name": "Project Manager",
                    "url": "https://acme.breezy.hr/p/p1-project-manager",
                    "location": {"name": "Remote"},
                    "type": {"name": "Full-Time"},
                    "company": {"name": "Acme"},
                }
            ],
            "acme.breezy.hr/p/p1-project-manager": page,
        }
    )
    (job,) = list(BreezySource(cfg("breezy"), client, ctx()).fetch())
    assert job.description == "Open to Africa." and job.employment_type == "Full-Time"


ZOHO_LIST = (
    '<input type="hidden" value="[{&#34;id&#34;:&#34;99&#34;,&#34;Posting_Title&#34;:'
    '&#34;Automation Project Manager&#34;,&#34;Publish&#34;:true}]" id="jobs">'
)
ZOHO_DETAIL = (
    "<script>\nvar jobs = JSON.parse('[{\\x22id\\x22:\\x2299\\x22,\\x22Posting_Title\\x22:"
    "\\x22Automation Project Manager\\x22,\\x22Remote_Job\\x22:true,\\x22Work_Timezone\\x22:"
    "\\x22Mon \\- Fri 9\\-5 EAT\\x22,\\x22Job_Description\\x22:\\x22<p>Kenya welcome<\\/p>\\x22}]');"
    "\n</script>"
)


def test_zoho_recruit_list_and_detail(make_client) -> None:
    client, _ = make_client(
        {
            "acme.zohorecruit.com/jobs/Careers": ZOHO_LIST,
            "acme.zohorecruit.com/jobs/Careers/99": ZOHO_DETAIL,
        }
    )
    (job,) = list(ZohoRecruitSource(cfg("zoho", kind="agency"), client, ctx()).fetch())
    assert job.remote is True
    assert job.description.startswith("Working hours: Mon - Fri 9-5 EAT")
    assert "Kenya welcome" in job.description


@pytest.mark.parametrize(
    ("url", "ats", "slug", "ref"),
    [
        ("https://job-boards.greenhouse.io/gitlab/jobs/123", "greenhouse", "gitlab", "123"),
        (
            "https://jobs.lever.co/acme/0f5a3c2e-1111-2222-3333-444455556666",
            "lever",
            "acme",
            "0f5a3c2e-1111-2222-3333-444455556666",
        ),
        (
            "https://apply.workable.com/360dialog-gmbh/j/4D3EDCFDEB/",
            "workable",
            "360dialog-gmbh",
            "4D3EDCFDEB",
        ),
        (
            "https://social-discovery-ventures.breezy.hr/p/c4584ec94f5d01-companion",
            "breezy",
            "social-discovery-ventures",
            "c4584ec94f5d01-companion",
        ),
        (
            "https://bruntwork.zohorecruit.com/jobs/Careers/655395000258154255/Enterprise",
            "zoho",
            "bruntwork",
            "655395000258154255",
        ),
    ],
)
def test_match_ats_url(url, ats, slug, ref) -> None:
    cls, s, r = match_ats_url(url)
    assert (cls.ats, s, r) == (ats, slug, ref)


def test_match_ats_url_rejects_aggregators() -> None:
    assert match_ats_url("https://himalayas.app/companies/x/jobs/y") is None


# ---------- discovery adapters


def test_remoteok_skips_legal_element_and_keeps_attribution(make_client) -> None:
    client, _ = make_client(
        {
            "remoteok.com/api": [
                {"legal": "API Terms of Service: Please link back"},
                {
                    "id": 7,
                    "position": "Automation Specialist",
                    "company": "Co",
                    "url": "https://remoteok.com/remote-jobs/7",
                    "description": "<p>Hi</p>",
                    "location": "Worldwide",
                    "apply_url": "https://remoteok.com/l/7",
                    "salary_min": 50000,
                    "salary_max": 70000,
                    "date": "2026-11-20T00:00:00+00:00",
                },
            ]
        }
    )
    (job,) = list(RemoteOKSource(cfg("remoteok", "discovery", None), client, ctx()).fetch())
    assert job.text_origin == "aggregator" and job.aggregator_location == "Worldwide"
    assert job.location_text is None  # an aggregator label is never stored as employer location
    assert job.attribution == "Source: Remote OK (https://remoteok.com/remote-jobs/7)"
    assert (job.salary_period, job.salary_currency) == ("year", "USD")


def test_himalayas_search_queries_and_dedupes_across_queries(make_client) -> None:
    j = {
        "guid": "https://himalayas.app/companies/co/jobs/pm",
        "title": "AI Project Manager",
        "companyName": "Co",
        "description": "<p>x</p>",
        "locationRestrictions": [],
        "applicationLink": "https://himalayas.app/companies/co/jobs/pm?utm_source=api",
        "pubDate": 1795000000,
        "salaryPeriod": "annual",
    }
    client, router = make_client({"himalayas.app/jobs/api/search": {"jobs": [j]}})
    src = HimalayasSource(
        cfg("himalayas", "discovery", None, queries=["a", "b"], country="KE"), client, ctx()
    )
    jobs = list(src.fetch())
    assert len(jobs) == 1 and jobs[0].aggregator_location == "Worldwide (Himalayas label)"
    assert jobs[0].salary_period == "year"
    assert all(c.url.params["country"] == "KE" for c in router.calls if "search" in c.url.path)


def test_jobicy(make_client) -> None:
    client, _ = make_client(
        {
            "jobicy.com/api/v2/remote-jobs": {
                "jobs": [
                    {
                        "id": 1,
                        "jobTitle": "Implementation Manager",
                        "companyName": "Co &amp; Sons",
                        "url": "https://jobicy.com/jobs/1",
                        "jobGeo": "EMEA",
                        "jobDescription": "<p>d</p>",
                        "jobType": ["full-time"],
                        "pubDate": "2026-11-20 10:00:00",
                    },
                ]
            }
        }
    )
    (job,) = list(JobicySource(cfg("jobicy", "discovery", None, tags=["x"]), client, ctx()).fetch())
    assert job.company == "Co & Sons" and job.aggregator_location == "EMEA"
    assert job.attribution.startswith("Source: Jobicy")


def test_myjobmag_rss_latin1_and_job_page(make_client) -> None:
    rss = (
        '<?xml version="1.0" encoding="iso-8859-1"?><rss><channel>'
        "<item><title><![CDATA[Project Analyst at Caf\xe9 Ltd]]></title>"
        "<link><![CDATA[https://www.myjobmag.co.ke/jobs/project-analyst]]></link>"
        "<pubDate>Tue, 24 Nov 2026 15:51:05 GMT</pubDate></item>"
        "<item><title><![CDATA[Jobs at Bundle Co]]></title><link>https://x</link></item>"
        "</channel></rss>"
    ).encode("latin-1")
    import httpx

    client, _ = make_client(
        {
            "www.myjobmag.co.ke/jobsxml.xml": httpx.Response(200, content=rss),
            "www.myjobmag.co.ke/jobs/project-analyst": '<div class="job-details"><p>Nairobi office.</p></div>',
        }
    )
    (job,) = list(MyJobMagSource(cfg("myjobmag", "discovery", None), client, ctx()).fetch())
    assert (job.title, job.company) == ("Project Analyst", "Caf\xe9 Ltd")
    assert job.description == "Nairobi office."


def test_reddit_skips_cleanly_without_credentials(make_client, monkeypatch) -> None:
    for k in ("CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"REDDIT_{k}", raising=False)
    client, router = make_client({})
    src = RedditSource(cfg("reddit", "community", None, subreddits=["forhire"]), client, ctx())
    assert list(src.fetch()) == []
    assert src.stats.errors == ["skipped: Reddit credentials not set in .env"]
    assert router.calls == []


def test_reddit_hiring_posts_and_scam_flags(make_client, monkeypatch) -> None:
    for k in ("CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PASSWORD"):
        monkeypatch.setenv(f"REDDIT_{k}", "x")
    posts = {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "1",
                        "title": "[Hiring] Automation Project Manager",
                        "selftext": "DM me on Telegram",
                        "permalink": "/r/forhire/1",
                        "author": "a",
                        "created_utc": 1795000000,
                    }
                },
                {
                    "data": {
                        "id": "2",
                        "title": "[For Hire] Automation expert",
                        "selftext": "",
                        "permalink": "/2",
                    }
                },
                {
                    "data": {
                        "id": "3",
                        "title": "[HIRING] Implementation lead",
                        "selftext": "Paid via contract",
                        "permalink": "/r/forhire/3",
                        "author": "b",
                    }
                },
            ]
        }
    }
    client, _ = make_client(
        {
            "www.reddit.com/api/v1/access_token": {"access_token": "t"},
            "oauth.reddit.com/r/forhire/new": posts,
        }
    )
    src = RedditSource(cfg("reddit", "community", None, subreddits=["forhire"]), client, ctx())
    jobs = list(src.fetch())
    assert [j.title for j in jobs] == ["Automation Project Manager", "Implementation lead"]
    assert jobs[0].flags == ["scam_risk"] and jobs[1].flags == []
    assert all(j.source_kind == "community" and j.text_origin == "community" for j in jobs)


def test_reddit_blocked_by_robots_when_not_overridden(make_client, monkeypatch) -> None:
    from jobagent.sourcing.http import RobotsDisallowed

    for k in ("CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PASSWORD"):
        monkeypatch.setenv(f"REDDIT_{k}", "x")
    client, _ = make_client({"www.reddit.com/robots.txt": "User-agent: *\nDisallow: /\n"})
    src = RedditSource(cfg("reddit", "community", None, subreddits=["forhire"]), client, ctx())
    with pytest.raises(RobotsDisallowed):
        list(src.fetch())


def test_sources_yaml_is_valid() -> None:
    from pathlib import Path

    from jobagent.sourcing.config import load_search_profile, load_sources

    root = Path(__file__).resolve().parents[1]
    s = load_sources(root / "config/sources.yaml")
    names = {x.name for x in s.sources}
    assert {"GitLab", "Pavago", "Bruntwork", "Himalayas", "Reddit"} <= names
    assert not next(x for x in s.sources if x.name == "Reddit").enabled
    assert {"Remotive", "We Work Remotely", "LinkedIn", "Indeed"} <= {
        x.name for x in s.not_supported
    }
    p = load_search_profile(root / "config/search_profile.yaml").title_prefilter
    assert p.passes("AI Implementation Specialist") and not p.passes("Customer Support Agent")
    assert not p.passes("Senior Software Engineer") and p.passes("Engineering Program Manager")
    json.dumps([x.model_dump() for x in s.sources])  # serialisable
