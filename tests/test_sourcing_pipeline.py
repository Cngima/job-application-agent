"""Stage 2: database migrations, resolver, de-duplication, run isolation and manual intake."""

from __future__ import annotations

import pytest

from jobagent.db import connect, migrate
from jobagent.sourcing.config import SearchProfile, SourceConfig, SourcesFile, TitlePrefilter
from jobagent.sourcing.models import Job
from jobagent.sourcing.pipeline import ManualIntakeError, add_manual, run_sourcing
from jobagent.sourcing.resolver import Resolver
from jobagent.sourcing.store import get_ats_lookup, insert_job, is_known

PROFILE = SearchProfile(title_prefilter=TitlePrefilter(include=["project", "automation"]))


def gh_job(i: int, title: str, content: str = "&lt;p&gt;Remote&lt;/p&gt;") -> dict:
    return {
        "id": i,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
        "content": content,
        "location": {"name": "Remote"},
    }


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def test_migrations_are_idempotent(conn) -> None:
    assert migrate(conn) == []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "sources", "ats_lookup", "sourcing_runs", "schema_version"} <= tables


def test_same_title_per_country_on_one_board_is_kept_but_cross_source_is_a_dupe(conn) -> None:
    a = Job(
        source="AJAIA",
        source_kind="employer_ats",
        title="AI Strategist",
        company="AJAIA",
        url="https://w/1",
    )
    insert_job(conn, a)
    same_board = a.model_copy(update={"url": "https://w/2"})
    other_source = a.model_copy(update={"url": "https://himalayas/x", "source": "Himalayas"})
    assert not is_known(conn, same_board)
    assert is_known(conn, other_source)
    assert is_known(conn, a.model_copy(update={"source": "x", "title": "different"}))  # same URL


def _sources(*cfgs: SourceConfig) -> SourcesFile:
    return SourcesFile(sources=list(cfgs))


def test_run_stores_new_jobs_and_skips_known_ones_next_time(conn, make_client) -> None:
    client, _ = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/acme/jobs": {
                "jobs": [gh_job(1, "Automation Project Manager"), gh_job(2, "Backend Developer")]
            }
        }
    )
    src = _sources(
        SourceConfig(name="Acme", adapter="greenhouse", slug="acme", kind="employer_ats")
    )
    (rep,) = run_sourcing(conn, src, PROFILE, client)
    assert (rep.status, rep.listed, rep.title_filtered, rep.new) == ("ok", 2, 1, 1)
    (rep2,) = run_sourcing(conn, src, PROFILE, client)
    assert (rep2.new, rep2.duplicates) == (0, 1)
    runs = conn.execute("SELECT new_jobs, status FROM sourcing_runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [(1, "ok"), (0, "ok")]


def test_dry_run_writes_nothing(conn, make_client) -> None:
    client, _ = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/acme/jobs": {
                "jobs": [gh_job(1, "Automation Project Manager")]
            }
        }
    )
    src = _sources(
        SourceConfig(name="Acme", adapter="greenhouse", slug="acme", kind="employer_ats")
    )
    (rep,) = run_sourcing(conn, src, PROFILE, client, dry_run=True)
    assert rep.new == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sourcing_runs").fetchone()[0] == 0


def test_a_failing_source_does_not_stop_the_run(conn, make_client) -> None:
    import httpx

    client, _ = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/broken/jobs": httpx.Response(500),
            "boards-api.greenhouse.io/v1/boards/acme/jobs": {
                "jobs": [gh_job(1, "Automation Project Manager")]
            },
            "blocked.example/robots.txt": "User-agent: *\nDisallow: /\n",
        },
        max_attempts=1,
    )
    src = _sources(
        SourceConfig(name="Broken", adapter="greenhouse", slug="broken", kind="employer_ats"),
        SourceConfig(name="Acme", adapter="greenhouse", slug="acme", kind="employer_ats"),
        SourceConfig(name="Off", adapter="remoteok", kind="discovery", enabled=False, notes="why"),
    )
    reps = {r.name: r for r in run_sourcing(conn, src, PROFILE, client)}
    assert reps["Broken"].status == "error" and "500" in reps["Broken"].error
    assert reps["Acme"].new == 1
    assert reps["Off"].status == "disabled"


def test_employer_sources_run_before_discovery_so_employer_text_wins(conn, make_client) -> None:
    client, _ = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/acme/jobs": {
                "jobs": [gh_job(1, "Automation Project Manager")]
            },
            "remoteok.com/api": [
                {"legal": "x"},
                {
                    "id": 1,
                    "position": "Automation Project Manager",
                    "company": "Acme",
                    "url": "https://remoteok.com/remote-jobs/1",
                },
            ],
        }
    )
    src = _sources(
        SourceConfig(name="RemoteOK", adapter="remoteok", kind="discovery"),
        SourceConfig(name="Acme", adapter="greenhouse", slug="acme", kind="employer_ats"),
    )
    reps = {r.name: r for r in run_sourcing(conn, src, PROFILE, client, resolve=False)}
    assert reps["Acme"].new == 1 and reps["RemoteOK"].duplicates == 1


# ---------- resolver


def test_resolver_follows_an_ats_apply_link(conn, make_client) -> None:
    client, _ = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/acme/jobs/5": gh_job(
                5, "AI Project Manager", "&lt;p&gt;Applicants in Kenya welcome&lt;/p&gt;"
            )
        }
    )
    lead = Job(
        source="RemoteOK",
        source_kind="discovery",
        title="AI Project Manager",
        company="Acme",
        url="https://remoteok.com/remote-jobs/5",
        apply_url="https://boards.greenhouse.io/acme/jobs/5",
        text_origin="aggregator",
        aggregator_location="Worldwide",
        description="agg text",
    )
    out = Resolver(conn, client).resolve(lead)
    assert out.text_origin == "employer" and out.description == "Applicants in Kenya welcome"
    assert out.employer_url == "https://boards.greenhouse.io/acme/jobs/5"
    assert out.aggregator_location == "Worldwide" and out.source == "RemoteOK"


def test_resolver_rejects_slug_owned_by_another_company_and_caches_negative(
    conn, make_client
) -> None:
    client, router = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/remote/jobs": {
                "jobs": [{"id": 1, "title": "AI Project Manager"}]
            },
            "boards-api.greenhouse.io/v1/boards/remote": {"name": "General Assembly Remote Jobs"},
        }
    )
    lead = Job(
        source="Himalayas",
        source_kind="discovery",
        title="AI Project Manager",
        company="Remote",
        url="https://himalayas.app/companies/remote/jobs/pm",
        text_origin="aggregator",
    )
    r = Resolver(conn, client)
    assert r.resolve(lead, "remote") is None
    assert get_ats_lookup(conn, "remote") is None  # negative result cached
    calls = len(router.calls)
    assert Resolver(conn, client).resolve(lead, "remote") is None
    assert len(router.calls) == calls  # cache hit: no new requests


def test_resolver_finds_board_by_company_name_and_matches_title(conn, make_client) -> None:
    client, _ = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/acmeco/jobs": {
                "jobs": [{"id": 9, "title": "AI Project Manager"}]
            },
            "boards-api.greenhouse.io/v1/boards/acmeco": {"name": "Acme Co"},
            "boards-api.greenhouse.io/v1/boards/acmeco/jobs/9": gh_job(
                9, "AI Project Manager", "&lt;p&gt;EMEA&lt;/p&gt;"
            ),
        }
    )
    lead = Job(
        source="Jobicy",
        source_kind="discovery",
        title="AI Project Manager ",
        company="Acme Co.",
        url="https://jobicy.com/jobs/1",
        text_origin="aggregator",
    )
    out = Resolver(conn, client).resolve(lead)
    assert out is not None and out.description == "EMEA"
    assert get_ats_lookup(conn, "acme") == ("greenhouse", "acmeco")


def test_resolver_respects_new_company_cap(conn, make_client) -> None:
    client, router = make_client({})
    r = Resolver(conn, client, max_new_companies=0)
    lead = Job(
        source="J", source_kind="discovery", title="T", company="Unknown Co", url="https://j/1"
    )
    assert r.resolve(lead) is None and router.calls == []


# ---------- manual intake


def test_manual_add_fetches_supported_ats_url(conn, make_client) -> None:
    client, _ = make_client(
        {"boards-api.greenhouse.io/v1/boards/acme/jobs/5": gh_job(5, "AI Project Manager")}
    )
    job, inserted = add_manual(
        conn, client, url="https://boards.greenhouse.io/acme/jobs/5", company="Acme"
    )
    assert inserted and job.source == "manual" and job.source_kind == "manual"
    assert job.text_origin == "employer"
    assert (
        add_manual(conn, client, url="https://boards.greenhouse.io/acme/jobs/5", company="Acme")[1]
        is False
    )


def test_manual_add_never_opens_linkedin_or_whatsapp_links(conn, make_client) -> None:
    client, router = make_client({})
    for url in (
        "https://www.linkedin.com/jobs/view/1",
        "https://wa.me/254700000000",
        "https://lnkd.in/abc",
        "https://uk.indeed.com/viewjob?jk=1",
    ):
        with pytest.raises(ManualIntakeError, match="never opens LinkedIn"):
            add_manual(conn, client, url=url)
    assert router.calls == []


def test_manual_add_follows_other_links_where_robots_allows(conn, make_client) -> None:
    import httpx

    page = (
        '<html><head><title>Ops Lead</title><script type="application/ld+json">'
        '{"@type":"JobPosting","title":"Automation Lead","hiringOrganization":{"name":"Acme",'
        '"sameAs":"https://acme.co.ke"},"jobLocationType":"TELECOMMUTE",'
        '"applicantLocationRequirements":{"@type":"Country","name":"Kenya"},'
        '"description":"<p>Remote across Kenya.</p>"}</script></head></html>'
    )
    client, _ = make_client(
        {
            "careers.acme.co.ke/jobs/1": httpx.Response(200, html=page),
            "blocked.example/robots.txt": "User-agent: *\nDisallow: /\n",
        }
    )
    job, inserted = add_manual(conn, client, url="https://careers.acme.co.ke/jobs/1")
    assert inserted and (job.title, job.company) == ("Automation Lead", "Acme")
    assert job.text_origin == "employer"  # hiring organisation's own domain
    assert "TELECOMMUTE" in job.location_text and "Kenya" in job.location_text
    with pytest.raises(ManualIntakeError, match="robots.txt"):
        add_manual(conn, client, url="https://blocked.example/job/9")


def test_manual_add_pasted_text_is_not_employer_text_unless_declared(conn, make_client) -> None:
    client, router = make_client({})
    job, _ = add_manual(
        conn,
        client,
        url="https://x.com/post/1",
        text="We hire in Kenya",
        title="Automation PM",
        company="Co",
    )
    assert job.text_origin == "pasted" and job.employer_url is None and router.calls == []
    job2, _ = add_manual(
        conn, client, text="Remote, Africa", title="Ops Lead", company="Co2", employer_text=True
    )
    assert job2.text_origin == "employer" and job2.url.startswith("manual://")
    job3, _ = add_manual(
        conn,
        client,
        text="Position: Automation Specialist\nCompany: Lakeside Ltd\n"
        "Location: Remote (Kenya)\nApply by 30 Nov",
    )
    assert (job3.title, job3.company, job3.aggregator_location) == (
        "Automation Specialist",
        "Lakeside Ltd",
        "Remote (Kenya)",
    )


def test_manual_add_pasted_scam_is_flagged(conn, make_client) -> None:
    client, _ = make_client({})
    job, _ = add_manual(
        conn,
        client,
        text=(
            "Safaricom is hiring Data Entry Clerks. Registration fee KES 500 via M-Pesa till number "
            "123456. Send your ID copy to safaricomjobs@gmail.com"
        ),
    )
    assert job.flags == ["scam_risk"]
    assert len(job.risk_reasons) >= 3


def test_names_match_rejects_containment_collisions() -> None:
    from jobagent.sourcing.resolver import names_match, slug_candidates

    assert not names_match("General Assembly Remote Jobs", "Remote")
    assert names_match("Invisible Technologies", "Invisible Technologies Inc.")
    assert names_match("Acme", "Acme Labs")
    assert names_match("PawaPay", "pawaPay")
    assert slug_candidates("Acme Co.", "acme-1") == ["acme-1", "acme", "acmeco", "acme-co"]


def test_known_postings_are_skipped_before_detail_fetch(conn, make_client) -> None:
    listing = {
        "name": "Agency",
        "jobs": [
            {"title": "Automation Specialist", "shortcode": "AAA111"},
            {"title": "Automation Specialist", "shortcode": "AAA111"},  # same posting, 2nd location
            {"title": "Project Coordinator", "shortcode": "BBB222"},
        ],
    }

    def detail(code: str) -> dict:
        return {"shortcode": code, "title": "x " + code, "description": "<p>d</p>"}

    client, router = make_client(
        {
            "apply.workable.com/api/v1/widget/accounts/ag": listing,
            "apply.workable.com/api/v1/accounts/ag/jobs/AAA111": detail("AAA111"),
            "apply.workable.com/api/v1/accounts/ag/jobs/BBB222": detail("BBB222"),
        }
    )
    src = _sources(SourceConfig(name="Ag", adapter="workable", slug="ag", kind="agency"))
    (rep,) = run_sourcing(conn, src, PROFILE, client)
    assert (rep.new, rep.duplicates) == (2, 1)
    assert router.paths().count("apply.workable.com/api/v1/accounts/ag/jobs/AAA111") == 1
    # Second run: both stored, so no detail requests at all, and the cap is not consumed.
    before = len(router.calls)
    (rep2,) = run_sourcing(conn, src, PROFILE, client, max_detail_fetches=1)
    assert (rep2.new, rep2.duplicates) == (0, 3)
    assert router.paths()[before:] == ["apply.workable.com/api/v1/widget/accounts/ag"]


def test_resolved_lead_is_not_resolved_again_next_run(conn, make_client) -> None:
    client, router = make_client(
        {
            "remoteok.com/api": [
                {"legal": "x"},
                {
                    "id": 1,
                    "position": "Automation Project Manager",
                    "company": "Acme",
                    "url": "https://remoteok.com/remote-jobs/1",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/5",
                },
            ],
            "boards-api.greenhouse.io/v1/boards/acme/jobs/5": gh_job(
                5, "Automation Project Manager"
            ),
        }
    )
    src = _sources(SourceConfig(name="RemoteOK", adapter="remoteok", kind="discovery"))
    (rep,) = run_sourcing(conn, src, PROFILE, client)
    assert (rep.new, rep.resolved) == (1, 1)
    before = len(router.calls)
    (rep2,) = run_sourcing(conn, src, PROFILE, client)
    assert (rep2.new, rep2.resolved, rep2.duplicates) == (0, 0, 1)
    assert router.paths()[before:] == ["remoteok.com/api"]


# ---------- age limit (jobs must not be older than a week)


def _iso(days_ago: float) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def test_jobs_older_than_the_limit_are_not_stored(conn, make_client) -> None:
    jobs = [
        gh_job(1, "Automation Project Manager"),
        gh_job(2, "Project Coordinator"),
        gh_job(3, "Automation Lead"),
    ]
    jobs[0]["first_published"] = _iso(2)
    jobs[1]["first_published"] = _iso(10)
    jobs[2].pop("first_published", None)  # undated
    client, _ = make_client({"boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": jobs}})
    src = _sources(
        SourceConfig(name="Acme", adapter="greenhouse", slug="acme", kind="employer_ats")
    )
    (rep,) = run_sourcing(conn, src, PROFILE, client, max_age_days=7)
    assert (rep.new, rep.too_old) == (2, 1)
    flags = dict(conn.execute("SELECT external_id, flags FROM jobs").fetchall())
    assert flags == {"1": "[]", "3": '["date_unknown"]'}
    assert conn.execute("SELECT too_old FROM sourcing_runs").fetchone()[0] == 1


def test_stale_postings_cost_no_detail_request(conn, make_client) -> None:
    listing = {
        "name": "Ag",
        "jobs": [
            {
                "title": "Automation Specialist",
                "shortcode": "OLD111",
                "published_on": _iso(30)[:10],
            },
            {"title": "Project Coordinator", "shortcode": "NEW222", "published_on": _iso(1)},
        ],
    }
    client, router = make_client(
        {
            "apply.workable.com/api/v1/widget/accounts/ag": listing,
            "apply.workable.com/api/v1/accounts/ag/jobs/NEW222": {
                "shortcode": "NEW222",
                "title": "Project Coordinator",
                "published": _iso(1),
            },
        }
    )
    src = _sources(SourceConfig(name="Ag", adapter="workable", slug="ag", kind="agency"))
    (rep,) = run_sourcing(conn, src, PROFILE, client, max_age_days=7)
    assert (rep.new, rep.too_old) == (1, 1)
    assert not any("OLD111" in p for p in router.paths())


def test_no_age_limit_when_disabled(conn, make_client) -> None:
    j = gh_job(1, "Automation Project Manager")
    j["first_published"] = _iso(400)
    client, _ = make_client({"boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [j]}})
    src = _sources(
        SourceConfig(name="Acme", adapter="greenhouse", slug="acme", kind="employer_ats")
    )
    (rep,) = run_sourcing(conn, src, PROFILE, client, max_age_days=None)
    assert rep.new == 1


def test_prune_old_jobs_keeps_recent_and_undated(conn) -> None:
    from datetime import datetime

    from jobagent.sourcing.store import prune_old_jobs

    for i, days in enumerate([1, 9, None]):
        insert_job(
            conn,
            Job(
                source="s",
                source_kind="employer_ats",
                title=f"T{i}",
                company="C",
                url=f"https://x/{i}",
                posted_at=None if days is None else datetime.fromisoformat(_iso(days)),
            ),
        )
    assert prune_old_jobs(conn, 7, dry_run=True) == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    assert prune_old_jobs(conn, 7) == 1
    assert sorted(r[0] for r in conn.execute("SELECT title FROM jobs")) == ["T0", "T2"]
