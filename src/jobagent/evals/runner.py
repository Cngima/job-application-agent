"""Run the router on the labelled eval set and score it.

The eval uses a throwaway in-memory database, so it never touches real decisions. It runs the
full router (rules, AI, policy checks), exactly as `jobagent route` does, minus the resolver
(the postings are frozen; no network).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jobagent.ai.provider import AIRunner
from jobagent.db import connect
from jobagent.evals.dataset import Label
from jobagent.routing.ai_router import PROMPT_VERSION
from jobagent.routing.router import route_jobs
from jobagent.sourcing.store import JOB_COLUMNS

CURRENT_DECISIONS = (
    "SELECT * FROM decisions d WHERE id = (SELECT MAX(id) FROM decisions WHERE job_id = d.job_id)"
)
ROUTES = ["APPLY", "CHECK_ELIGIBILITY", "REVIEW", "SKIP", "LINKEDIN_LEAD"]


@dataclass
class EvalResult:
    provider: str
    model: str | None
    prompt_version: str
    n: int
    route_correct: int
    elig_n: int
    elig_correct: int
    confusion: dict[str, dict[str, int]]
    disagreements: list[dict]
    ai_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float
    flips: int = 0  # jobs whose route changed between repeats
    repeats: int = 1
    unconfirmed_included: bool = False
    per_job: dict[str, list[str]] = field(default_factory=dict)

    @property
    def route_accuracy(self) -> float:
        return self.route_correct / self.n if self.n else 0.0

    @property
    def eligibility_accuracy(self) -> float:
        return self.elig_correct / self.elig_n if self.elig_n else 0.0


def _insert_frozen(conn, item: dict) -> None:
    row = {c: None for c in JOB_COLUMNS}
    row.update({k: v for k, v in item.items() if k in row})
    row["canonical_url"] = item.get("employer_url") or item["url"]
    row["dedupe_key"] = f"eval|{item['id']}"
    row["first_seen_at"] = datetime.now(UTC).isoformat()
    row["posted_at"] = None  # frozen postings must not age out of the eval
    row["tags"] = "[]"
    row["flags"] = json.dumps(item.get("flags") or [])
    row["risk_reasons"] = json.dumps(item.get("risk_reasons") or [])
    if isinstance(row.get("remote"), bool):
        row["remote"] = int(row["remote"])
    conn.execute(
        f"INSERT INTO jobs ({', '.join(JOB_COLUMNS)}) VALUES ({', '.join('?' * len(JOB_COLUMNS))})",
        [row[c] for c in JOB_COLUMNS],
    )


def run_eval(
    postings: list[dict],
    labels: dict[str, Label],
    profile,
    routing_cfg,
    make_runner,
    *,
    batch_size: int = 5,
    repeats: int = 1,
    include_unconfirmed: bool = False,
) -> EvalResult:
    """`make_runner(conn)` returns a fresh AIRunner bound to the eval database."""
    scored = {i: lab for i, lab in labels.items() if lab.confirmed or include_unconfirmed}
    items = [p for p in postings if p["id"] in scored]
    started = datetime.now(UTC)
    runs: list[dict[str, dict]] = []
    calls = tok_in = tok_out = 0
    provider = model = None
    for _ in range(repeats):
        conn = connect(":memory:")
        for item in items:
            _insert_frozen(conn, item)
        conn.commit()
        runner: AIRunner = make_runner(conn)
        route_jobs(conn, profile, routing_cfg, runner, batch_size=batch_size)
        runs.append({r["job_id"]: dict(r) for r in conn.execute(CURRENT_DECISIONS)})
        c = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "MAX(provider), MAX(model) FROM ai_calls"
        ).fetchone()
        calls, tok_in, tok_out = calls + c[0], tok_in + c[1], tok_out + c[2]
        provider, model = c[3] or runner.provider.name, c[4] or model
        conn.close()

    final = runs[-1]
    confusion: dict[str, dict[str, int]] = {r: Counter() for r in ROUTES}
    route_ok = elig_n = elig_ok = 0
    disagreements = []
    for item in items:
        lab = scored[item["id"]]
        got = final.get(item["id"], {})
        route = got.get("route", "UNDECIDED")
        confusion[lab.route][route] += 1
        route_ok += route == lab.route
        verdict = got.get("eligibility_verdict")
        if lab.eligibility is not None:
            elig_n += 1
            elig_ok += verdict == lab.eligibility
        if route != lab.route or (lab.eligibility is not None and verdict != lab.eligibility):
            disagreements.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "company": item["company"],
                    "label_route": lab.route,
                    "got_route": route,
                    "label_eligibility": lab.eligibility,
                    "got_eligibility": verdict,
                    "label_reason": lab.reason,
                    "stage": got.get("stage"),
                    "quote": got.get("quoted_location_text"),
                    "reasons": json.loads(got.get("reasons") or "[]"),
                }
            )
    per_job = {i["id"]: [r.get(i["id"], {}).get("route", "UNDECIDED") for r in runs] for i in items}
    flips = sum(1 for routes in per_job.values() if len(set(routes)) > 1)
    return EvalResult(
        provider=provider or "",
        model=model,
        prompt_version=PROMPT_VERSION,
        n=len(items),
        route_correct=route_ok,
        elig_n=elig_n,
        elig_correct=elig_ok,
        confusion={k: dict(v) for k, v in confusion.items() if v},
        disagreements=disagreements,
        ai_calls=calls,
        input_tokens=tok_in,
        output_tokens=tok_out,
        seconds=(datetime.now(UTC) - started).total_seconds(),
        flips=flips,
        repeats=repeats,
        unconfirmed_included=include_unconfirmed
        and not all(lab.confirmed for lab in scored.values()),
        per_job=per_job,
    )


def report_markdown(res: EvalResult, title: str) -> str:
    lines = [
        f"# {title}",
        "",
        f"- Date: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        f"- Provider: {res.provider} ({res.model or 'n/a'}), prompt {res.prompt_version}",
        f"- Jobs scored: {res.n}"
        + (" (includes UNCONFIRMED labels)" if res.unconfirmed_included else ""),
        f"- **Routing accuracy: {res.route_correct}/{res.n} = {res.route_accuracy:.0%}**",
        f"- **Eligibility accuracy: {res.elig_correct}/{res.elig_n} = "
        f"{res.eligibility_accuracy:.0%}**",
        f"- AI calls: {res.ai_calls}, tokens in/out: {res.input_tokens:,}/{res.output_tokens:,}, "
        f"time: {res.seconds:.0f} s",
    ]
    if res.repeats > 1:
        lines.append(
            f"- Stability: {res.flips} of {res.n} jobs changed route across {res.repeats} runs"
        )
    lines += [
        "",
        "## Confusion (rows = label, columns = router)",
        "",
        "| label \\ router | " + " | ".join(ROUTES) + " | UNDECIDED |",
        "|---" * (len(ROUTES) + 2) + "|",
    ]
    for lab_route, row in res.confusion.items():
        lines.append(
            f"| {lab_route} | "
            + " | ".join(str(row.get(r, 0)) for r in ROUTES)
            + f" | {row.get('UNDECIDED', 0)} |"
        )
    lines += ["", f"## Disagreements ({len(res.disagreements)})", ""]
    for d in res.disagreements:
        lines.append(f"### {d['title']} @ {d['company']}")
        lines.append(
            f"- Label: {d['label_route']} / {d['label_eligibility']}. "
            f"Router: {d['got_route']} / {d['got_eligibility']} ({d['stage']})"
        )
        lines.append(f"- Label reason: {d['label_reason']}")
        if d["quote"]:
            lines.append(f'- Router quote: "{d["quote"][:200]}"')
        for r in d["reasons"][:4]:
            lines.append(f"- Router: {r}")
        lines.append("")
    if res.repeats > 1 and res.flips:
        lines += ["## Jobs that changed route between runs", ""]
        lines += [f"- {i}: {' -> '.join(r)}" for i, r in res.per_job.items() if len(set(r)) > 1]
    return "\n".join(lines) + "\n"


def save_report(res: EvalResult, out_dir: Path, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    path = out_dir / f"{stamp}_{res.provider}_{res.prompt_version}.md"
    path.write_text(report_markdown(res, title), encoding="utf-8")
    return path
