"""Stage 3b: the eval set loads, only confirmed labels are scored, results are correct."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jobagent.ai.provider import AIRunner
from jobagent.ai.providers import FakeProvider
from jobagent.evals.dataset import Label, load_labels, load_postings
from jobagent.evals.runner import report_markdown, run_eval
from jobagent.routing.fake import fake_dispatch
from jobagent.sourcing.config import load_search_profile

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data/sample/evals"
PROFILE = load_search_profile(ROOT / "config/search_profile.yaml")
CFG = SimpleNamespace(fx_to_usd={"USD": 1.0, "KES": 0.0077}, max_desc_chars=4000)


def fake_runner(conn):
    return AIRunner(FakeProvider(fallback=fake_dispatch), conn, max_calls=20)


def test_sample_eval_set_is_valid_and_fictional() -> None:
    postings = load_postings(SAMPLE / "postings.jsonl")
    labels = load_labels(SAMPLE / "labels.yaml")
    assert {p["id"] for p in postings} == set(labels)
    assert all(lab.confirmed for lab in labels.values())
    assert all("example.com" in p["url"] for p in postings)


def test_sample_eval_runs_end_to_end_with_fake_provider() -> None:
    res = run_eval(
        load_postings(SAMPLE / "postings.jsonl"),
        load_labels(SAMPLE / "labels.yaml"),
        PROFILE,
        CFG,
        fake_runner,
    )
    assert res.n == 8 and res.route_correct == 8 and res.ai_calls == 1
    md = report_markdown(res, "t")
    assert "Routing accuracy: 8/8" in md and "\u2014" not in md


def test_disagreements_and_unconfirmed_labels() -> None:
    postings = load_postings(SAMPLE / "postings.jsonl")
    labels = load_labels(SAMPLE / "labels.yaml")
    labels["s01"] = Label(
        route="SKIP", eligibility="EXCLUDED", reason="deliberately wrong", confirmed=True
    )
    labels["s02"] = labels["s02"].model_copy(update={"confirmed": False})
    res = run_eval(postings, labels, PROFILE, CFG, fake_runner)
    assert res.n == 7  # the unconfirmed label is not scored
    (d,) = res.disagreements
    assert (d["id"], d["label_route"], d["got_route"]) == ("s01", "SKIP", "APPLY")
    assert res.confusion["SKIP"]["APPLY"] == 1
    assert run_eval(postings, labels, PROFILE, CFG, fake_runner, include_unconfirmed=True).n == 8


def test_repeats_measure_route_flips() -> None:
    postings = load_postings(SAMPLE / "postings.jsonl")
    labels = load_labels(SAMPLE / "labels.yaml")
    state = {"n": 0}

    def flaky(s, p, schema):
        out = fake_dispatch(s, p, schema)
        state["n"] += 1
        if state["n"] == 2:  # second run: the AI changes its mind on one job
            next(j for j in out["jobs"] if j["job_id"] == "s01")["route"] = "REVIEW"
        return out

    res = run_eval(
        postings,
        labels,
        PROFILE,
        CFG,
        lambda conn: AIRunner(FakeProvider(fallback=flaky), conn),
        repeats=2,
    )
    assert res.flips == 1 and res.repeats == 2


@pytest.mark.skipif(not (ROOT / "data/private/evals/labels.yaml").exists(), reason="private set")
def test_private_eval_files_match() -> None:
    d = ROOT / "data/private/evals"
    assert {p["id"] for p in load_postings(d / "postings.jsonl")} == set(
        load_labels(d / "labels.yaml")
    )
