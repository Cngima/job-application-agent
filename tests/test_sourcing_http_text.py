"""Stage 2: polite HTTP client and text helpers."""

from __future__ import annotations

import httpx
import pytest

from jobagent.sourcing.http import HostUnavailable, PoliteClient, RobotsDisallowed
from jobagent.sourcing.models import Job
from jobagent.sourcing.text import canonical_url, dedupe_key, div_by_class, html_to_text

ROBOTS_BLOCK_API = "User-agent: *\nDisallow: /api/\n"


def test_robots_disallow_blocks_request(make_client) -> None:
    client, router = make_client({"x.com/robots.txt": ROBOTS_BLOCK_API, "x.com/api/jobs": []})
    with pytest.raises(RobotsDisallowed):
        client.get("https://x.com/api/jobs")
    assert router.paths() == ["x.com/robots.txt"]  # the API itself was never called


def test_robots_404_means_allow_and_is_cached(make_client) -> None:
    client, router = make_client({"x.com/a": {"ok": 1}, "x.com/b": {"ok": 2}})
    assert client.get_json("https://x.com/a") == {"ok": 1}
    assert client.get_json("https://x.com/b") == {"ok": 2}
    assert router.paths().count("x.com/robots.txt") == 1


def test_robots_5xx_skips_host(make_client) -> None:
    client, _ = make_client({"x.com/robots.txt": httpx.Response(503)})
    with pytest.raises(HostUnavailable):
        client.get("https://x.com/a")
    with pytest.raises(HostUnavailable):  # remembered for the rest of the run
        client.get("https://x.com/b")


def test_user_agent_identifies_the_project(make_client) -> None:
    client, router = make_client({"x.com/a": {}})
    client.get("https://x.com/a")
    assert "job-application-agent" in router.calls[-1].headers["User-Agent"]
    assert "github.com/Cngima/job-application-agent" in router.calls[-1].headers["User-Agent"]


def test_per_host_rate_limit() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    client = PoliteClient(
        "ua",
        min_interval=2.0,
        sleep=sleep,
        clock=lambda: now[0],
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    client.get("https://a.com/1")  # robots.txt, wait 2 s, request
    client.get("https://a.com/2")  # wait 2 s
    client.get("https://b.com/1")  # b.com robots.txt goes straight away; wait 2 s; request
    assert sleeps == [2.0, 2.0, 2.0]
    assert client.requests_made == 5


def test_retry_after_429_then_success() -> None:
    attempts = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []
    client = PoliteClient(
        "ua", min_interval=0, sleep=sleeps.append, transport=httpx.MockTransport(handler)
    )
    assert client.get_json("https://x.com/a") == {"ok": True}
    assert 7.0 in sleeps


def test_transport_errors_retry_then_raise() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("boom")

    client = PoliteClient(
        "ua", min_interval=0, sleep=lambda s: None, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(httpx.ConnectError):
        client.get("https://x.com/a")


# ---------- text helpers


def test_html_to_text_handles_double_escaped_greenhouse_html() -> None:
    raw = "&lt;p&gt;Remote &amp;amp; flexible&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Kenya&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(raw) == "Remote & flexible\n\n- Kenya"


def test_html_to_text_drops_scripts() -> None:
    assert html_to_text("<p>Hi</p><script>var x=1</script>") == "Hi"


def test_canonical_url_strips_tracking_but_keeps_ids() -> None:
    a = canonical_url("HTTPS://Himalayas.app/companies/x/jobs/y/?utm_source=api#top")
    assert a == "https://himalayas.app/companies/x/jobs/y"
    b = canonical_url("https://about.gitlab.com/jobs/apply/?gh_jid=123&gh_src=abc")
    assert b == "https://about.gitlab.com/jobs/apply?gh_jid=123"


def test_dedupe_key_ignores_case_punctuation_and_company_suffix() -> None:
    assert dedupe_key("AI Project Manager!", "Talent Inc.") == dedupe_key(
        "ai project manager", "Talent"
    )


def test_div_by_class_handles_nesting() -> None:
    page = '<div class="top"><div class="description"><p>A</p><div>B</div></div><p>C</p></div>'
    assert html_to_text(div_by_class(page, "description")) == "A\n\nB"  # C is outside


def test_job_id_is_stable_and_prefers_employer_url() -> None:
    j = Job(
        source="s",
        source_kind="discovery",
        title="T",
        company="C",
        url="https://agg.com/j/1?utm_source=x",
        employer_url="https://boards.greenhouse.io/c/jobs/9",
    )
    assert j.canonical_url == "https://boards.greenhouse.io/c/jobs/9"
    assert (
        j.id
        == Job(
            source="other",
            source_kind="employer_ats",
            title="T",
            company="C",
            url="https://boards.greenhouse.io/c/jobs/9/",
        ).id
    )
