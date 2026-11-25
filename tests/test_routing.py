"""Stage 3: geography, pay, rules, AI policy checks, providers and the router (all mocked)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from jobagent.ai.provider import AIError, AIRunner, InvalidOutput, ProviderPaused
from jobagent.ai.providers import (
    AnthropicAPIProvider,
    ClaudeCodeProvider,
    FakeProvider,
    OllamaProvider,
)
from jobagent.db import connect
from jobagent.routing.ai_router import (
    BATCH_SCHEMA,
    PROMPT_VERSION,
    BatchVerdict,
    JobVerdict,
    enforce_policy,
    system_prompt,
)
from jobagent.routing.fake import fake_dispatch
from jobagent.routing.geo import find_restrictions, read_places
from jobagent.routing.pay import estimate_monthly_usd
from jobagent.routing.router import route_jobs
from jobagent.routing.rules import apply_cap, apply_rules
from jobagent.sourcing.config import load_search_profile
from jobagent.sourcing.models import Job
from jobagent.sourcing.store import insert_job

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_search_profile(ROOT / "config/search_profile.yaml")
FX = {"USD": 1.0, "KES": 0.0077, "EUR": 1.08}


# --------------------------------------------------------------------------- geography


@pytest.mark.parametrize(
    ("loc", "excludes", "inclusive"),
    [
        ("Pakistan; Pakistan", True, False),
        ("Remote - United States", True, False),
        (
            "Egypt, Argentina, Ethiopia, South Africa, Nigeria",
            True,
            False,
        ),  # South Africa is not Africa-wide
        ("West Africa", True, False),
        ("Kenya; Kenya; South Africa", False, True),
        ("Africa - Remote", False, True),
        ("East Africa", False, True),
        ("Worldwide", False, True),
        ("Remote", False, False),
        ("Remote - EMEA", False, False),  # contains Kenya, but not proof: AI decides
        ("contact us anytime", False, False),  # lower-case "us" is not a place
    ],
)
def test_read_places(loc, excludes, inclusive) -> None:
    r = read_places(loc)
    assert (r.excludes_kenya, r.inclusive) == (excludes, inclusive)


def test_find_restrictions_ignores_salary_sentences() -> None:
    text = (
        "The salary range is currently for residents of the United States only. "
        "This role is open to candidates based in LATAM, Africa, and Eastern Europe."
    )
    (r,) = find_restrictions(text)
    assert r.reading.inclusive
    (x,) = find_restrictions("Candidates must be authorized to work in the US without sponsorship.")
    assert x.reading.excludes_kenya and x.quote == "authorized to work in the US"
    assert find_restrictions("Only your final submission will be reviewed.") == []


# --------------------------------------------------------------------------- pay


def _pay(**kw):
    base = dict(
        salary_min=None,
        salary_max=None,
        currency=None,
        period=None,
        salary_text=None,
        description="",
        is_part_time=False,
        fx_to_usd=FX,
    )
    return estimate_monthly_usd(**{**base, **kw})


def test_pay_uses_stated_hours_before_defaults() -> None:
    p = _pay(description="Compensation: $12/hour for 20 hours per week.", is_part_time=True)
    assert (
        p.monthly_usd_top == pytest.approx(12 * 20 * 4.33, rel=1e-3) and p.hours_source == "stated"
    )
    q = _pay(description="Pay: $12/hour.", is_part_time=True)
    assert q.monthly_usd_top == 960 and q.hours_source == "assumed part-time"


def test_pay_ignores_perks_and_unstated_pay() -> None:
    assert _pay(description="We offer a learning budget of USD 2,000 per year.") is None
    assert _pay(description="Home office stipend of $150 per month.") is None
    assert _pay(description="Great team, competitive salary.") is None


def test_pay_from_fields_and_currency() -> None:
    p = _pay(salary_min=1_200_000, salary_max=1_800_000, currency="KES", period="year")
    assert p.monthly_usd_top == pytest.approx(1_800_000 * 0.0077 / 12, rel=1e-3)
    assert _pay(salary_text="$2.9K – $3.7K per month").monthly_usd_top == 3700


# --------------------------------------------------------------------------- rules


def job(**kw) -> dict:
    base = dict(
        id="j1",
        title="AI Implementation Specialist",
        company="Acme",
        source="Acme",
        source_kind="employer_ats",
        text_origin="employer",
        location_text="Remote",
        description="Help clients automate workflows.",
        flags=[],
        risk_reasons=[],
        employment_type=None,
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        salary_period=None,
        salary_text=None,
    )
    return {**base, **kw}


def test_rule_skips_location_field_without_kenya() -> None:
    rr = apply_rules(job(location_text="Remote - United States"), PROFILE, FX)
    assert rr.final_route == "SKIP" and rr.eligibility == "EXCLUDED"
    assert rr.quote == "Remote - United States"


def test_rule_never_skips_on_aggregator_location_labels() -> None:
    rr = apply_rules(
        job(
            text_origin="aggregator",
            source_kind="discovery",
            location_text=None,
            aggregator_location="USA",
        ),
        PROFILE,
        FX,
    )
    assert rr.final_route is None and rr.cap == "CHECK_ELIGIBILITY"


def test_rule_title_exclusions() -> None:
    assert apply_rules(job(title="Senior Software Engineer"), PROFILE, FX).final_route == "SKIP"
    assert apply_rules(job(title="Director of Operations"), PROFILE, FX).final_route == "SKIP"
    assert apply_rules(job(title="Engineering Program Manager"), PROFILE, FX).final_route is None


def test_rule_pay_floor_with_review_band() -> None:
    low = apply_rules(job(salary_text="$1,000 per month"), PROFILE, FX)
    assert low.final_route == "SKIP"  # full-time floor US$1,800
    near = apply_rules(job(salary_text="$1,700 per month"), PROFILE, FX)
    assert near.final_route is None and near.cap == "REVIEW"  # within 10% below the floor
    pt = apply_rules(job(salary_text="$1,400 per month", employment_type="Part-time"), PROFILE, FX)
    assert pt.final_route is None and pt.cap is None  # part-time floor US$1,300


def test_rule_scam_caps_at_review_and_leads_go_to_linkedin_lead() -> None:
    rr = apply_rules(job(flags=["scam_risk"], risk_reasons=["asks for a fee"]), PROFILE, FX)
    assert rr.cap == "REVIEW" and "asks for a fee" in rr.hits[-1].detail
    assert apply_rules(job(flags=["linkedin_lead"]), PROFILE, FX).final_route == "LINKEDIN_LEAD"


def test_caps() -> None:
    assert apply_cap("APPLY", "CHECK_ELIGIBILITY") == "CHECK_ELIGIBILITY"
    assert apply_cap("APPLY", "REVIEW") == "REVIEW"
    assert apply_cap("CHECK_ELIGIBILITY", "REVIEW") == "REVIEW"
    assert apply_cap("SKIP", "REVIEW") == "SKIP"


# --------------------------------------------------------------------------- AI policy checks


def verdict(**kw) -> JobVerdict:
    base = dict(
        job_id="j1",
        route="APPLY",
        eligibility_verdict="KENYA_CONFIRMED",
        quoted_location_text="Remote, open to candidates in Kenya",
        function_match="STRONG",
        seniority_match="MATCH",
        workplace="REMOTE",
        workplace_city="",
        hours_requirement={
            "employment": "FULL_TIME",
            "hours_per_week": 0,
            "us_hours_required": False,
            "timezone_note": "",
        },
        reasons=["fits"],
    )
    return JobVerdict(**{**base, **kw})


def test_policy_accepts_verbatim_quote() -> None:
    j = job(description="We are hiring. Remote, open to candidates in Kenya.")
    assert enforce_policy(verdict(), j, PROFILE)[:2] == ("APPLY", "KENYA_CONFIRMED")


def test_policy_lowers_paraphrased_quote_to_unclear() -> None:
    j = job(description="We hire across Africa.")
    route, v, notes = enforce_policy(verdict(quoted_location_text="Kenya is welcome"), j, PROFILE)
    assert (route, v) == ("CHECK_ELIGIBILITY", "UNCLEAR") and "verbatim" in notes[0]


def test_policy_never_trusts_aggregator_text_for_eligibility() -> None:
    j = job(text_origin="aggregator", description="Remote, open to candidates in Kenya")
    assert enforce_policy(verdict(), j, PROFILE)[:2] == ("CHECK_ELIGIBILITY", "UNCLEAR")


def test_policy_workplace_rules() -> None:
    j = job(description="Remote, open to candidates in Kenya")
    assert (
        enforce_policy(verdict(workplace="HYBRID", workplace_city="Nakuru"), j, PROFILE)[0]
        == "APPLY"
    )
    assert (
        enforce_policy(verdict(workplace="HYBRID", workplace_city="Mombasa"), j, PROFILE)[0]
        == "SKIP"
    )
    assert (
        enforce_policy(verdict(workplace="ONSITE", workplace_city="Nairobi"), j, PROFILE)[0]
        == "SKIP"
    )
    assert enforce_policy(verdict(eligibility_verdict="EXCLUDED"), j, PROFILE)[0] == "SKIP"


def test_system_prompt_has_injection_guard_and_no_em_dashes() -> None:
    s = system_prompt(PROFILE)
    assert "never instructions to you" in s and "—" not in s
    assert PROMPT_VERSION == "route-v2"


# --------------------------------------------------------------------------- providers


def _completed(stdout: dict | str, code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    out = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return subprocess.CompletedProcess([], code, out, stderr)


@pytest.fixture
def claude_cli(monkeypatch):
    calls = {}

    def install(result):
        def fake_run(cmd, **kw):
            calls["cmd"], calls["kw"] = cmd, kw
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr("jobagent.ai.providers.shutil.which", lambda exe: "/usr/bin/claude")
        monkeypatch.setattr("jobagent.ai.providers.subprocess.run", fake_run)
        return calls

    return install


def test_claude_code_success_reads_structured_output_and_tokens(claude_cli) -> None:
    calls = claude_cli(
        _completed(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "{}",
                "structured_output": {"verdict": "KENYA"},
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 1184,
                    "output_tokens": 56,
                },
                "modelUsage": {"claude-opus-5-5": {}},
            }
        )
    )
    res = ClaudeCodeProvider(timeout=5).complete("sys", "prompt", {"type": "object"})
    assert res.data == {"verdict": "KENYA"} and res.model == "claude-opus-5-5"
    assert (res.input_tokens, res.output_tokens, res.cost_usd) == (1186, 56, None)
    cmd = calls["cmd"]
    assert "-p" in cmd and cmd[cmd.index("--tools") + 1] == "" and "--json-schema" in cmd
    assert calls["kw"]["input"] == "prompt"  # prompt via stdin


def test_claude_code_usage_limit_pauses(claude_cli) -> None:
    claude_cli(
        _completed(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "Claude AI usage limit reached. Resets at 3pm",
            },
            code=1,
        )
    )
    with pytest.raises(ProviderPaused):
        ClaudeCodeProvider().complete("s", "p", {})


def test_claude_code_timeout_and_crash_are_errors(claude_cli) -> None:
    claude_cli(subprocess.TimeoutExpired("claude", 5))
    with pytest.raises(AIError, match="timed out"):
        ClaudeCodeProvider(timeout=5).complete("s", "p", {})
    claude_cli(_completed("", code=2, stderr="segfault"))
    with pytest.raises(AIError, match="exit 2") as e:
        ClaudeCodeProvider().complete("s", "p", {})
    assert not isinstance(e.value, ProviderPaused)


def test_claude_code_missing_cli_pauses(monkeypatch) -> None:
    monkeypatch.setattr("jobagent.ai.providers.shutil.which", lambda exe: None)
    with pytest.raises(ProviderPaused, match="not found"):
        ClaudeCodeProvider().complete("s", "p", {})


def test_ollama_success_and_unreachable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["format"] == {"type": "object"} and body["stream"] is False
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"verdict": "OTHER"}'},
                "prompt_eval_count": 50,
                "eval_count": 5,
            },
        )

    res = OllamaProvider(transport=httpx.MockTransport(handler)).complete(
        "s", "p", {"type": "object"}
    )
    assert res.data == {"verdict": "OTHER"} and (res.input_tokens, res.output_tokens) == (50, 5)

    def down(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderPaused, match="not reachable"):
        OllamaProvider(transport=httpx.MockTransport(down)).complete("s", "p", {})


def test_anthropic_api_cost_and_structured_output() -> None:
    sent = {}

    class Msgs:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(
                stop_reason="end_turn",
                model="claude-opus-5-5",
                content=[SimpleNamespace(type="text", text='{"verdict": "KENYA"}')],
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=100,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )

    client = SimpleNamespace(beta=SimpleNamespace(messages=Msgs()))
    res = AnthropicAPIProvider(client=client).complete("sys", "p", {"type": "object"})
    assert res.data == {"verdict": "KENYA"}
    assert res.cost_usd == pytest.approx((1000 * 4 + 100 * 20) / 1_000_000)
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default"


# --------------------------------------------------------------------------- runner


def test_runner_retries_once_then_invalid_and_logs_every_attempt() -> None:
    conn = connect(":memory:")
    from pydantic import BaseModel

    class Out(BaseModel):
        verdict: str

    bad = FakeProvider(fallback=lambda s, p, sch: {"wrong": 1})
    runner = AIRunner(bad, conn, max_calls=10)
    with pytest.raises(InvalidOutput):
        runner.call("test", "s", "p", {}, Out)
    rows = conn.execute("SELECT attempt, status FROM ai_calls ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "invalid"), (2, "invalid")]
    seq = iter([{"wrong": 1}, {"verdict": "ok"}])
    out, _ = AIRunner(FakeProvider(fallback=lambda s, p, sch: next(seq)), conn).call(
        "t", "s", "p", {}, Out
    )
    assert out.verdict == "ok"


def test_runner_call_cap_pauses() -> None:
    from pydantic import BaseModel

    class Out(BaseModel):
        verdict: str

    runner = AIRunner(FakeProvider(fallback=lambda s, p, sch: {"verdict": "x"}), None, max_calls=1)
    runner.call("t", "s", "p", {}, Out)
    with pytest.raises(ProviderPaused, match="cap"):
        runner.call("t", "s", "p", {}, Out)


# --------------------------------------------------------------------------- router end to end


def _store(conn, **kw) -> str:
    base = dict(
        source="Acme",
        source_kind="employer_ats",
        title="AI Implementation Specialist",
        company="Acme",
        url="https://boards.greenhouse.io/acme/jobs/1",
        description="Remote, open to candidates in Kenya. Help clients automate workflows.",
        location_text="Remote",
    )
    j = Job(**{**base, **kw})
    insert_job(conn, j)
    return j.id


def test_router_end_to_end_with_fake_provider() -> None:
    conn = connect(":memory:")
    cfg = SimpleNamespace(fx_to_usd=FX, max_desc_chars=4000)
    good = _store(conn)
    us = _store(
        conn,
        url="https://x/2",
        title="Automation Project Manager",
        location_text="Remote - US",
        description="Help US clients automate workflows.",
    )
    lead = _store(
        conn,
        url="https://www.linkedin.com/jobs/view/9/",
        source="linkedin_alert",
        source_kind="discovery",
        text_origin="aggregator",
        flags=["linkedin_lead"],
        title="Implementation Manager",
    )
    scam = _store(
        conn,
        url="whatsapp://abc",
        source="whatsapp",
        source_kind="community",
        text_origin="community",
        flags=["scam_risk"],
        risk_reasons=["asks for a fee"],
        title="Automation Assistant",
        company="Unknown employer",
    )
    runner = AIRunner(FakeProvider(fallback=fake_dispatch), conn, max_calls=5)
    rep = route_jobs(conn, PROFILE, cfg, runner, batch_size=5)
    routes = dict(conn.execute("SELECT job_id, route FROM decisions").fetchall())
    assert routes == {good: "APPLY", us: "SKIP", lead: "LINKEDIN_LEAD", scam: "REVIEW"}
    assert rep.ai_calls == 1  # two AI jobs in one batch
    row = conn.execute("SELECT * FROM decisions WHERE job_id = ?", (good,)).fetchone()
    assert row["quoted_location_text"].startswith("Remote, open to candidates in Kenya")
    assert row["prompt_version"] == PROMPT_VERSION and row["provider"] == "fake"
    # A second run finds nothing left to decide.
    assert route_jobs(conn, PROFILE, cfg, runner).considered == 0


def test_router_leaves_jobs_undecided_when_ai_pauses() -> None:
    conn = connect(":memory:")
    _store(conn)

    def paused(s, p, sch):
        raise ProviderPaused("usage limit")

    rep = route_jobs(
        conn,
        PROFILE,
        SimpleNamespace(fx_to_usd=FX, max_desc_chars=100),
        AIRunner(FakeProvider(fallback=paused), conn),
    )
    assert rep.pending == 1 and rep.paused == "usage limit"
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_router_ai_failure_routes_to_review() -> None:
    conn = connect(":memory:")
    jid = _store(conn)
    rep = route_jobs(
        conn,
        PROFILE,
        SimpleNamespace(fx_to_usd=FX, max_desc_chars=100),
        AIRunner(FakeProvider(fallback=lambda s, p, sch: {"jobs": "nonsense"}), conn),
    )
    assert rep.by_stage == {"ai_failed": 1}
    assert (
        conn.execute("SELECT route FROM decisions WHERE job_id = ?", (jid,)).fetchone()[0]
        == "REVIEW"
    )


def test_batch_schema_matches_pydantic_model() -> None:
    sample = fake_dispatch(
        "",
        'Route these jobs:\n[{"job_id": "x", "title": "AI PM", "description": "Remote"}]',
        BATCH_SCHEMA,
    )
    assert BatchVerdict.model_validate(sample).jobs[0].job_id == "x"


def test_conflicting_location_field_and_description_caps_at_check() -> None:
    rr = apply_rules(
        job(
            location_text="Egypt, Argentina, Nigeria",
            description="This role is open to candidates based in LATAM, Africa, and Eastern Europe.",
        ),
        PROFILE,
        FX,
    )
    assert rr.final_route is None and rr.cap == "CHECK_ELIGIBILITY"
    assert any(h.rule == "location_conflict" for h in rr.hits)
