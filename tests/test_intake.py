"""Intake: scam checks, WhatsApp parsing, link following, LinkedIn alerts, Gmail, inbox."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jobagent.db import connect
from jobagent.intake.gmail import SCOPES, _plain_text, list_alert_messages
from jobagent.intake.linkedin import parse_alert
from jobagent.intake.pages import fetch_advert, never_fetch
from jobagent.intake.processor import (
    add_board_to_sources,
    process_inbox,
    process_linkedin_leads,
    process_whatsapp_text,
    retry_pending_leads,
)
from jobagent.intake.scam import scam_reasons
from jobagent.intake.whatsapp import parse_post, split_posts
from jobagent.sourcing.config import TitlePrefilter, load_search_profile, load_sources
from jobagent.sourcing.resolver import Resolver

ROOT = Path(__file__).resolve().parents[1]
TITLE_OK = TitlePrefilter(include=["project", "automation", "implementation", "coordinator"]).passes


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def gh_job(i: int, title: str, content: str = "&lt;p&gt;Remote, Kenya welcome&lt;/p&gt;") -> dict:
    return {
        "id": i,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/lakeside/jobs/{i}",
        "content": content,
        "location": {"name": "Remote"},
        "first_published": datetime.now(UTC).isoformat(),
    }


# --------------------------------------------------------------------------- scam checks


@pytest.mark.parametrize(
    ("text", "company", "expect"),
    [
        ("Pay a registration fee of KES 1,000 to secure your slot.", None, "fee or M-Pesa"),
        ("Send KES 500 via M-Pesa to 0712 345 678 to be shortlisted", None, "fee or M-Pesa"),
        ("Use Paybill 123456 for the training kit", None, "fee or M-Pesa"),
        ("Apply on WhatsApp: 0712 345 678. Urgent!", None, "personal phone or WhatsApp"),
        ("Apply here: https://wa.me/254712345678", None, "personal phone or WhatsApp"),
        ("Send CV to hr.safaricom@gmail.com", "Safaricom PLC", "free email address"),
        ("Earn up to $300 daily typing from home", None, "unrealistic pay"),
        ("Data entry: $150/hour, no experience", None, "unrealistic pay"),
        (
            "Send a copy of your national ID and KRA PIN before the interview",
            None,
            "ID or bank documents",
        ),
    ],
)
def test_scam_checks_flag(text, company, expect) -> None:
    assert any(expect in r for r in scam_reasons(text, company))


@pytest.mark.parametrize(
    "text",
    [
        "Project Coordinator at Lakeside Ltd. Apply: https://boards.greenhouse.io/lakeside/jobs/1",
        "Product Manager, M-Pesa Platform. Apply via careers.example.co.ke. Pay: KES 250,000 per month.",
        "Send your CV to jobs@lakeside.co.ke. Enquiries: 0712 345 678.",
        "Rate: $25/hour, part-time, remote across Africa. Apply at https://lakeside.example/jobs",
    ],
)
def test_scam_checks_leave_normal_posts_alone(text) -> None:
    assert scam_reasons(text, "Lakeside Ltd") == []


def test_free_email_only_flagged_for_a_named_company() -> None:
    assert scam_reasons("Email jane.doe@gmail.com", "Unknown employer") == []


# --------------------------------------------------------------------------- WhatsApp parsing


EXPORT = """24/11/2026, 09:12 - Remote Jobs KE: *HIRING* 🚀
Position: Automation Project Manager
Company: Lakeside Logistics
Location: Remote (Kenya)
Apply: https://boards.greenhouse.io/lakeside/jobs/7
24/11/2026, 09:30 - Remote Jobs KE: Good morning everyone!
24/11/2026, 10:05 - Remote Jobs KE: <Media omitted>
[25/11/2026, 14:02:11] Remote Jobs KE: Sunbird Clinics is hiring a Project Coordinator
Send your CV to careers@sunbird.example by Friday. Nairobi, hybrid.
"""

COPIED = """🔥 Implementation Specialist at Riverbank Ltd 🔥
Remote, Africa-wide. Apply: https://riverbank.example/careers/impl
------------------------------
We are hiring a Workflow Automation Coordinator!
Company: Delta Co
DM 0712 345 678 on WhatsApp to apply. Registration fee KES 300.



Implementation Specialist at Riverbank Ltd
Remote, Africa-wide. Apply: https://riverbank.example/careers/impl
"""


def test_split_chat_export_keeps_job_posts_and_drops_chatter() -> None:
    posts = split_posts(EXPORT)
    assert [(p.title, p.company) for p in posts] == [
        ("Automation Project Manager", "Lakeside Logistics"),
        ("Project Coordinator", "Sunbird Clinics"),
    ]
    assert posts[0].location == "Remote (Kenya)"
    assert posts[0].links == ["https://boards.greenhouse.io/lakeside/jobs/7"]
    assert posts[0].posted_at == datetime(2026, 11, 24, 9, 12, tzinfo=UTC)
    assert posts[1].emails == ["careers@sunbird.example"]


def test_split_copied_posts_by_separator_and_blank_lines_and_dedupes() -> None:
    posts = split_posts(COPIED)
    assert len(posts) == 2  # third block is a re-post of the first (emoji aside)
    assert (posts[0].title, posts[0].company) == ("Implementation Specialist", "Riverbank Ltd")
    assert posts[1].company == "Delta Co" and posts[1].phones


def test_parse_post_strips_whatsapp_formatting() -> None:
    p = parse_post("*Job Title:* _Senior Implementation Manager_\n*Company:* ~Acme~ Ltd")
    assert (p.title, p.company) == ("Senior Implementation Manager", "Acme Ltd")


# --------------------------------------------------------------------------- link following


def test_never_fetch_hosts() -> None:
    for u in (
        "https://www.linkedin.com/jobs/view/1",
        "https://lnkd.in/x",
        "https://wa.me/2547",
        "https://t.me/jobs",
        "https://ke.indeed.com/viewjob",
        "mailto:hr@x.com",
    ):
        assert never_fetch(u)
    assert not never_fetch("https://careers.example.co.ke/jobs/1")


def test_redirects_are_checked_against_robots_on_every_hop(make_client) -> None:
    client, router = make_client(
        {
            "bit.example/abc": httpx.Response(
                301, headers={"location": "https://www.linkedin.com/jobs/view/9"}
            ),
            "bit.example/def": httpx.Response(302, headers={"location": "https://shy.example/job"}),
            "shy.example/robots.txt": "User-agent: *\nDisallow: /\n",
            "bit.example/ghi": httpx.Response(
                302, headers={"location": "https://boards.greenhouse.io/lakeside/jobs/5"}
            ),
            "boards-api.greenhouse.io/v1/boards/lakeside/jobs/5": gh_job(5, "Automation Lead"),
        }
    )
    assert fetch_advert(client, "https://bit.example/abc") is None  # redirected to LinkedIn
    assert not any("linkedin" in p for p in router.paths())
    assert fetch_advert(client, "https://bit.example/def") is None  # robots on the 2nd host
    assert "shy.example/job" not in router.paths()
    ad = fetch_advert(client, "https://bit.example/ghi")  # shortener -> ATS API
    assert ad.is_employer and ad.title == "Automation Lead"


def test_aggregator_page_text_is_not_employer_text(make_client) -> None:
    page = "<html><title>Job board</title><nav>menu</nav><p>Implementation Lead, Remote.</p></html>"
    client, _ = make_client({"jobsboard.example/j/1": httpx.Response(200, html=page)})
    ad = fetch_advert(client, "https://jobsboard.example/j/1")
    assert not ad.is_employer and "menu" not in ad.description


# --------------------------------------------------------------------------- WhatsApp processing


def test_whatsapp_batch_end_to_end(conn, make_client) -> None:
    client, _ = make_client(
        {
            "boards-api.greenhouse.io/v1/boards/lakeside/jobs/7": gh_job(
                7, "Automation Project Manager"
            ),
        }
    )
    text = EXPORT.replace("24/11/2026", datetime.now(UTC).strftime("%d/%m/%Y")).replace(
        "25/11/2026", datetime.now(UTC).strftime("%d/%m/%Y")
    )
    rep = process_whatsapp_text(conn, client, text, title_ok=TITLE_OK)
    assert (rep.posts, rep.new, rep.employer_found) == (2, 2, 1)
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM jobs")}
    gh = rows["Automation Project Manager"]
    assert gh["source"] == "whatsapp" and gh["text_origin"] == "employer"
    assert "Original WhatsApp post" in gh["description"]
    assert rows["Project Coordinator"]["text_origin"] == "community"
    rep2 = process_whatsapp_text(conn, client, text, title_ok=TITLE_OK)
    assert (rep2.new, rep2.duplicates) == (0, 2)


def test_whatsapp_scam_post_is_flagged_with_reasons(conn, make_client) -> None:
    client, _ = make_client({})
    rep = process_whatsapp_text(conn, client, COPIED, title_ok=TITLE_OK, max_age_days=None)
    assert rep.scam_flagged == 1
    row = conn.execute("SELECT flags, risk_reasons FROM jobs WHERE company = 'Delta Co'").fetchone()
    assert "scam_risk" in row["flags"] and "fee or M-Pesa" in row["risk_reasons"]


def test_whatsapp_old_posts_are_skipped(conn, make_client) -> None:
    client, _ = make_client({})
    old = (datetime.now(UTC) - timedelta(days=12)).strftime("%d/%m/%Y")
    rep = process_whatsapp_text(
        conn,
        client,
        f"{old}, 08:00 - Ch: Lakeside is hiring a Project Coordinator. Apply by email jobs@lakeside.example",
        max_age_days=7,
    )
    assert (rep.too_old, rep.new) == (1, 0)


def test_inbox_moves_processed_files_and_skips_identical_content(
    conn, make_client, tmp_path
) -> None:
    client, _ = make_client({})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "monday.txt").write_text(COPIED, encoding="utf-8")
    (inbox / "notes.md").write_text("ignored", encoding="utf-8")
    (rep,) = process_inbox(conn, client, inbox, title_ok=TITLE_OK, max_age_days=None)
    assert rep.new == 2
    assert not (inbox / "monday.txt").exists()
    assert len(list((inbox / "processed").glob("*_monday.txt"))) == 1
    assert (inbox / "notes.md").exists()
    (inbox / "copy.txt").write_text(COPIED, encoding="utf-8")
    (rep2,) = process_inbox(conn, client, inbox, title_ok=TITLE_OK, max_age_days=None)
    assert rep2.errors == ["already processed (same content)"] and rep2.new == 0


def test_inbox_dry_run_moves_and_writes_nothing(conn, make_client, tmp_path) -> None:
    client, _ = make_client({})
    (tmp_path / "a.txt").write_text(COPIED, encoding="utf-8")
    process_inbox(conn, client, tmp_path, dry_run=True, title_ok=TITLE_OK, max_age_days=None)
    assert (tmp_path / "a.txt").exists()
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --------------------------------------------------------------------------- LinkedIn alerts


ALERT = """Your job alert for Implementation Manager in Kenya
Automation Project Manager
Lakeside Logistics
Nairobi, Kenya
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/1111111111/?trackingId=abc&refId=xyz

---------------------------------------------------------

Implementation Coordinator
Quiet Harbour Ltd
Kenya
This company is actively hiring
View job: https://www.linkedin.com/comm/jobs/view/2222222222/?trackingId=def

---------------------------------------------------------

Senior Software Engineer
Lakeside Logistics
Remote
View job: https://www.linkedin.com/comm/jobs/view/3333333333/?trackingId=ghi

---------------------------------------------------------

See all jobs on LinkedIn: https://www.linkedin.com/comm/jobs/search-results/?keywords=x
Manage your job alerts: https://www.linkedin.com/comm/jobs/alerts?x=1
"""


def test_parse_alert_extracts_clean_leads() -> None:
    leads = parse_alert(ALERT)
    assert [(lead.title, lead.company, lead.location) for lead in leads] == [
        ("Automation Project Manager", "Lakeside Logistics", "Nairobi, Kenya"),
        ("Implementation Coordinator", "Quiet Harbour Ltd", "Kenya"),
        ("Senior Software Engineer", "Lakeside Logistics", "Remote"),
    ]
    assert leads[0].url == "https://www.linkedin.com/jobs/view/1111111111/"  # tracking removed


def _lakeside_routes() -> dict:
    return {
        "boards-api.greenhouse.io/v1/boards/lakesidelogistics/jobs": {
            "jobs": [{"id": 7, "title": "Automation Project Manager"}]
        },
        "boards-api.greenhouse.io/v1/boards/lakesidelogistics": {"name": "Lakeside Logistics"},
        "boards-api.greenhouse.io/v1/boards/lakesidelogistics/jobs/7": gh_job(
            7, "Automation Project Manager"
        ),
    }


def test_linkedin_leads_traced_to_employer_board_or_kept_as_leads(
    conn, make_client, tmp_path
) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text((ROOT / "config/sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    client, router = make_client(_lakeside_routes())
    now = datetime.now(UTC)
    leads = [
        lead.__class__(lead.job_id, lead.title, lead.company, lead.location, now)
        for lead in parse_alert(ALERT)
    ]
    rep = process_linkedin_leads(
        conn, Resolver(conn, client), leads, sources_path=sources, title_ok=TITLE_OK
    )
    assert (rep.employer_found, rep.linkedin_leads, rep.title_filtered) == (1, 1, 1)
    assert not any("linkedin.com" in p for p in router.paths())  # LinkedIn is never fetched
    rows = {r["company"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["Lakeside Logistics"]["text_origin"] == "employer"
    assert rows["Quiet Harbour Ltd"]["flags"] == '["linkedin_lead"]'
    assert rows["Quiet Harbour Ltd"]["url"] == "https://www.linkedin.com/jobs/view/2222222222/"
    added = [s for s in load_sources(sources).sources if s.slug == "lakesidelogistics"]
    assert added and added[0].adapter == "greenhouse" and added[0].kind == "employer_ats"
    assert rep.boards_added == ["Lakeside Logistics (greenhouse:lakesidelogistics)"]
    rep2 = process_linkedin_leads(
        conn, Resolver(conn, client), leads, sources_path=sources, title_ok=TITLE_OK
    )
    assert rep2.new == 0 and rep2.boards_added == []


def test_capped_lookups_are_retried_next_run(conn, make_client) -> None:
    client, _ = make_client(_lakeside_routes())
    now = datetime.now(UTC)
    (lead,) = [
        x.__class__(x.job_id, x.title, x.company, x.location, now) for x in parse_alert(ALERT)[:1]
    ]
    process_linkedin_leads(
        conn, Resolver(conn, client, max_new_companies=0), [lead], sources_path=None
    )
    assert "lookup_pending" in conn.execute("SELECT flags FROM jobs").fetchone()[0]
    rep = process_linkedin_leads(conn, Resolver(conn, client), [], sources_path=None)
    retry_pending_leads(conn, Resolver(conn, client), None, rep)
    row = conn.execute("SELECT text_origin, flags FROM jobs").fetchone()
    assert (row["text_origin"], row["flags"], rep.employer_found) == ("employer", "[]", 1)


def test_add_board_to_sources_keeps_comments_and_skips_existing(tmp_path) -> None:
    sources = tmp_path / "sources.yaml"
    original = (ROOT / "config/sources.yaml").read_text(encoding="utf-8")
    sources.write_text(original, encoding="utf-8")
    assert add_board_to_sources(sources, "New Co", "lever", "newco")
    assert not add_board_to_sources(sources, "New Co", "lever", "newco")
    assert not add_board_to_sources(sources, "GitLab", "greenhouse", "gitlab")
    text = sources.read_text(encoding="utf-8")
    assert "# To add a company you found on hiring.cafe" in text  # comments survive
    assert text.count("added automatically") == 1


# --------------------------------------------------------------------------- Gmail (read-only)


def test_gmail_scope_is_read_only() -> None:
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmail:
    """Minimal stand-in for the Gmail API client. It has no send/modify/delete methods."""

    def __init__(self, messages: dict[str, dict]):
        self.messages_data = messages
        self.queries: list[str] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, pageToken=None, maxResults=50):
        self.queries.append(q)
        return _Exec({"messages": [{"id": i} for i in self.messages_data]})

    def get(self, userId, id, format):
        return _Exec(self.messages_data[id])


def _gmail_msg(mid: str, text: str) -> dict:
    data = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return {
        "id": mid,
        "internalDate": str(int(datetime.now(UTC).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "LinkedIn <jobalerts-noreply@linkedin.com>"},
                {"name": "Subject", "value": "Jobs for you"},
            ],
            "parts": [
                {"mimeType": "text/html", "body": {"data": "PGI+eDwvYj4"}},
                {"mimeType": "text/plain", "body": {"data": data}},
            ],
        },
    }


def test_list_alert_messages_reads_plain_text_part() -> None:
    svc = FakeGmail({"m1": _gmail_msg("m1", ALERT)})
    (msg,) = list(
        list_alert_messages(svc, ["jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"], 7)
    )
    assert svc.queries == [
        "from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com) newer_than:7d"
    ]
    assert len(parse_alert(msg.text)) == 3
    assert _plain_text({"mimeType": "text/html", "body": {"data": "eA"}}) == ""


# --------------------------------------------------------------------------- config


def test_location_settings_in_search_profile() -> None:
    prof = load_search_profile(ROOT / "config/search_profile.yaml")
    assert prof.kenya_onsite_ok is False
    assert prof.kenya_hybrid_cities == ["Nairobi", "Nakuru"]


def test_inbox_is_gitignored() -> None:
    assert "inbox/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_post_that_only_says_send_your_cv_is_still_a_job() -> None:
    (p,) = split_posts(
        "🔥 Implementation Coordinator at Riverbank Health 🔥\n"
        "Hybrid, Nairobi (2 days in office). Send your CV to careers@riverbank.example"
    )
    assert (p.title, p.company) == ("Implementation Coordinator", "Riverbank Health")


def test_sample_whatsapp_file_parses() -> None:
    posts = split_posts((ROOT / "data/sample/whatsapp_example.txt").read_text(encoding="utf-8"))
    assert [p.company for p in posts] == ["Lakeside Logistics", "Riverbank Health", "Safaricom"]
