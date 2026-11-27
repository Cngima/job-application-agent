"""Command-line entry point: `jobagent`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv

from jobagent import __version__
from jobagent.config import Config, load_config
from jobagent.logging_setup import setup_logging

app = typer.Typer(help="Find, route, score, draft and track job applications. Never submits.")
evidence_app = typer.Typer(help="Build and check the evidence bank.")
app.add_typer(evidence_app, name="evidence")

_state: dict[str, Config] = {}


def cfg() -> Config:
    return _state["config"]


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    profile: str = typer.Option(
        None, "--profile", "-p", help="sample (public demo) or private (your real data)"
    ),
    config: Path = typer.Option(Path("config.yaml"), help="Path to config.yaml"),
) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging("DEBUG" if verbose else "INFO")
    load_dotenv()
    _state["config"] = load_config(config).with_profile(profile)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


# --------------------------------------------------------------------------- evidence


@evidence_app.command("build")
def evidence_build(
    cv_dir: Path = typer.Option(..., exists=True, file_okay=False, help="Folder with CV .docx"),
    pattern: str = typer.Option("*.docx", help="Glob for CV files inside cv_dir"),
    out: Path = typer.Option(Path("data/private/evidence_bank.yaml")),
    overrides: Path = typer.Option(
        Path("data/private/evidence_overrides.yaml"),
        help="Review decisions to apply after parsing (skipped if the file does not exist)",
    ),
    force: bool = typer.Option(False, help="Overwrite an existing bank"),
) -> None:
    """Parse CVs into an evidence bank plus a review report. Never overwrites without --force."""
    from jobagent.evidence.builder import build_bank, write_bank
    from jobagent.evidence.overrides import load_overrides

    paths = sorted(p for p in cv_dir.glob(pattern) if not p.name.startswith("~$"))
    if not paths:
        raise typer.BadParameter(f"no files match {pattern!r} in {cv_dir}")
    if out.exists() and not force:
        out = out.with_suffix(".new.yaml")
        typer.echo(f"Existing bank kept. Writing the fresh build to {out} for comparison.")
    ov = load_overrides(overrides) if overrides.exists() else None
    if ov:
        typer.echo(f"Applying review decisions from {overrides}")
    bank, report = build_bank(paths, ov)
    write_bank(bank, out)
    report_path = out.with_name(out.stem.replace(".new", "") + "_review.md")
    report_path.write_text(report, encoding="utf-8")
    typer.echo(
        f"{len(paths)} CVs -> {len(bank.facts)} facts, {len(bank.roles)} roles, "
        f"{len(bank.allowed_tools)} tools.\nBank: {out}\nReview: {report_path}"
    )


@evidence_app.command("check")
def evidence_check(path: Path = typer.Argument(Path("data/private/evidence_bank.yaml"))) -> None:
    """Validate an evidence bank file against the schema."""
    from jobagent.evidence.builder import load_bank

    bank = load_bank(path)
    typer.echo(
        f"OK: {len(bank.facts)} facts, {len(bank.allowed_tools)} tools, "
        f"{len(bank.never_claim)} never_claim rules."
    )


# --------------------------------------------------------------------------- sourcing


def _client():
    from jobagent.sourcing.http import PoliteClient

    h = cfg().http
    return PoliteClient(h.user_agent, h.min_seconds_between_requests, h.timeout_seconds)


@app.command()
def source(
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch and report, write nothing"),
    only: list[str] = typer.Option(None, "--only", help="Run only these source names"),
    no_resolve: bool = typer.Option(False, help="Skip tracing discovery jobs to employers"),
    sources_file: Path = typer.Option(Path("config/sources.yaml")),
    search_profile: Path = typer.Option(Path("config/search_profile.yaml")),
) -> None:
    """Stage 2: read every enabled source and store new jobs."""
    from jobagent.db import connect
    from jobagent.sourcing.config import load_search_profile, load_sources
    from jobagent.sourcing.pipeline import run_sourcing

    c = cfg()
    conn = connect(c.db_path)
    client = _client()
    profile = load_search_profile(search_profile)
    try:
        reports = run_sourcing(
            conn,
            load_sources(sources_file),
            profile,
            client,
            only=only or None,
            dry_run=dry_run,
            resolve=not no_resolve,
            max_detail_fetches=c.sourcing.max_detail_fetches_per_source,
            max_new_companies=c.sourcing.max_new_companies_per_run,
            max_age_days=profile.max_age_days,
        )
    finally:
        client.close()
    head = (
        f"{'source':24} {'status':9} {'listed':>6} {'title-':>6} {'old':>4} {'dupes':>5} "
        f"{'new':>4} {'resolv':>6} {'reqs':>4}"
    )
    typer.echo(("DRY RUN: nothing written\n" if dry_run else "") + head + "\n" + "-" * len(head))
    typer.echo(f"(title- = dropped by title, old = posted over {profile.max_age_days} days ago)")
    for r in reports:
        typer.echo(
            f"{r.name[:24]:24} {r.status:9} {r.listed:>6} {r.title_filtered:>6} {r.too_old:>4} "
            f"{r.duplicates:>5} {r.new:>4} {r.resolved:>6} {r.requests:>4}"
        )
    for r in reports:
        for line in ([f"error: {r.error}"] if r.error else []) + [n for n in r.notes if n]:
            typer.echo(f"  {r.name}: {line}")
    typer.echo(f"\nTotal new: {sum(r.new for r in reports)}   database: {c.db_path}")
    for r in reports:
        if r.sample:
            typer.echo(f"\n{r.name}:\n  " + "\n  ".join(r.sample))


def _intake_report(r) -> None:
    typer.echo(
        f"{r.name}: {r.posts} posts, {r.new} new, {r.duplicates} duplicates, {r.too_old} too old, "
        f"{r.title_filtered} off-target titles, {r.links_followed} links followed, "
        f"{r.employer_found} traced to the employer, {r.scam_flagged} scam-flagged"
        + (f", {r.linkedin_leads} LinkedIn leads to check by hand" if r.linkedin_leads else "")
    )
    for b in r.boards_added:
        typer.echo(f"  added to config/sources.yaml: {b}")
    for e in r.errors:
        typer.echo(f"  note: {e}")
    for s in r.sample:
        typer.echo(f"  {s}")


def _resolver(conn, client, sources_file: Path = Path("config/sources.yaml")):
    from jobagent.sourcing.config import load_sources
    from jobagent.sourcing.resolver import Resolver, known_boards_from_sources

    return Resolver(
        conn,
        client,
        max_new_companies=cfg().sourcing.max_new_companies_per_run,
        known_boards=known_boards_from_sources(load_sources(sources_file)),
    )


@app.command()
def add(
    url_or_text: str = typer.Argument(None, help="A posting URL, or the posting text itself"),
    batch: Path = typer.Option(
        None, exists=True, dir_okay=False, help="A WhatsApp text file with many posts"
    ),
    text_file: Path = typer.Option(None, exists=True, dir_okay=False, help="One pasted posting"),
    title: str = typer.Option(None),
    company: str = typer.Option(None),
    employer_text: bool = typer.Option(
        False, help="The pasted text is copied from the employer's own posting"
    ),
    search_profile: Path = typer.Option(Path("config/search_profile.yaml")),
) -> None:
    """Add jobs found anywhere: one URL, one pasted post, or a whole WhatsApp batch file."""
    from jobagent.db import connect
    from jobagent.intake.processor import process_whatsapp_text
    from jobagent.sourcing.config import load_search_profile
    from jobagent.sourcing.pipeline import ManualIntakeError, add_manual

    conn = connect(cfg().db_path)
    client = _client()
    try:
        if batch:
            prof = load_search_profile(search_profile)
            rep = process_whatsapp_text(
                conn,
                client,
                batch.read_text(encoding="utf-8-sig"),
                name=batch.name,
                resolver=_resolver(conn, client),
                title_ok=prof.title_prefilter.passes,
                max_age_days=prof.max_age_days,
                max_links=cfg().intake.follow_links_per_post,
            )
            _intake_report(rep)
            return
        is_url = bool(url_or_text) and url_or_text.strip().lower().startswith(
            ("http://", "https://")
        )
        url = url_or_text.strip() if is_url else None
        body = (
            text_file.read_text(encoding="utf-8-sig")
            if text_file
            else (None if is_url else url_or_text)
        )
        try:
            job, inserted = add_manual(
                conn,
                client,
                url=url,
                text=body,
                title=title,
                company=company,
                employer_text=employer_text,
            )
        except ManualIntakeError as e:
            raise typer.BadParameter(str(e)) from e
    finally:
        client.close()
    state = "Added" if inserted else "Already tracked"
    typer.echo(f"{state}: {job.title} @ {job.company} (id {job.id}, text: {job.text_origin})")
    for reason in job.risk_reasons:
        typer.echo(f"  SCAM RISK: {reason}")


@app.command()
def inbox(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only; move and write nothing"),
    search_profile: Path = typer.Option(Path("config/search_profile.yaml")),
) -> None:
    """Process every new .txt file in the WhatsApp inbox folder."""
    from jobagent.db import connect
    from jobagent.intake.processor import process_inbox
    from jobagent.sourcing.config import load_search_profile

    c = cfg()
    prof = load_search_profile(search_profile)
    conn = connect(c.db_path)
    client = _client()
    try:
        reports = process_inbox(
            conn,
            client,
            c.intake.inbox_dir,
            dry_run=dry_run,
            resolver=_resolver(conn, client),
            title_ok=prof.title_prefilter.passes,
            max_age_days=prof.max_age_days,
            max_links=c.intake.follow_links_per_post,
        )
    finally:
        client.close()
    if not reports:
        typer.echo(f"No new .txt files in {c.intake.inbox_dir.resolve()}")
    for r in reports:
        _intake_report(r)


gmail_app = typer.Typer(help="Read-only Gmail access for LinkedIn job alert emails.")
app.add_typer(gmail_app, name="gmail")


@gmail_app.command("auth")
def gmail_auth() -> None:
    """One-time browser consent for gmail.readonly. Stores a token in data/private/."""
    from jobagent.intake.gmail import GmailNotConfigured, authorise

    g = cfg().intake.gmail
    try:
        authorise(g.client_secret, g.token)
    except GmailNotConfigured as e:
        raise typer.BadParameter(str(e)) from e
    typer.echo(f"Read-only Gmail token saved to {g.token}. Set intake.gmail.enabled: true.")


@gmail_app.command("alerts")
def gmail_alerts(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only; remember nothing"),
    from_file: Path = typer.Option(
        None, exists=True, dir_okay=False, help="Parse a saved alert email (plain text) instead"
    ),
    sources_file: Path = typer.Option(Path("config/sources.yaml")),
    search_profile: Path = typer.Option(Path("config/search_profile.yaml")),
) -> None:
    """Read new LinkedIn job alert emails and trace each lead to the company's own board."""
    from jobagent.db import connect, now_iso
    from jobagent.intake.gmail import GmailNotConfigured, build_service, list_alert_messages
    from jobagent.intake.linkedin import parse_alert
    from jobagent.intake.processor import (
        IntakeReport,
        process_linkedin_leads,
        retry_pending_leads,
    )
    from jobagent.sourcing.config import load_search_profile

    c = cfg()
    prof = load_search_profile(search_profile)
    conn = connect(c.db_path)
    client = _client()
    resolver = _resolver(conn, client)
    rep = IntakeReport("linkedin_alert")
    kw = dict(
        sources_path=sources_file,
        title_ok=prof.title_prefilter.passes,
        max_age_days=prof.max_age_days,
        dry_run=dry_run,
        rep=rep,
    )
    try:
        if not dry_run:
            retry_pending_leads(conn, resolver, sources_file, rep)
        if from_file:
            process_linkedin_leads(
                conn, resolver, parse_alert(from_file.read_text(encoding="utf-8")), **kw
            )
        else:
            if not c.intake.gmail.enabled:
                raise typer.BadParameter(
                    "Gmail is off: run `jobagent gmail auth`, then set "
                    "intake.gmail.enabled: true in config.yaml"
                )
            try:
                service = build_service(c.intake.gmail.token)
            except GmailNotConfigured as e:
                raise typer.BadParameter(str(e)) from e
            g = c.intake.gmail
            for msg in list_alert_messages(
                service, g.linkedin_senders, g.lookback_days, g.max_messages_per_run
            ):
                if conn.execute(
                    "SELECT 1 FROM gmail_messages WHERE message_id = ?", (msg.id,)
                ).fetchone():
                    continue
                leads = parse_alert(msg.text, msg.received_at)
                process_linkedin_leads(conn, resolver, leads, **kw)
                if not dry_run:
                    conn.execute(
                        "INSERT INTO gmail_messages "
                        "(message_id, received_at, sender, processed_at, leads) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (msg.id, msg.received_at.isoformat(), msg.sender, now_iso(), len(leads)),
                    )
                    conn.commit()
    finally:
        client.close()
    _intake_report(rep)


@app.command()
def intake(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Daily intake: the WhatsApp inbox, then LinkedIn alerts (if Gmail is set up)."""
    inbox(dry_run=dry_run, search_profile=Path("config/search_profile.yaml"))
    if cfg().intake.gmail.enabled:
        gmail_alerts(
            dry_run=dry_run,
            from_file=None,
            sources_file=Path("config/sources.yaml"),
            search_profile=Path("config/search_profile.yaml"),
        )
    else:
        typer.echo("LinkedIn alerts: Gmail not set up (see README, 'Gmail setup').")


@app.command()
def prune(
    dry_run: bool = typer.Option(False, "--dry-run", help="Count only, delete nothing"),
    search_profile: Path = typer.Option(Path("config/search_profile.yaml")),
) -> None:
    """Remove stored jobs older than max_age_days (search profile). Backs up the DB first."""
    import shutil
    from datetime import datetime

    from jobagent.db import connect
    from jobagent.sourcing.config import load_search_profile
    from jobagent.sourcing.store import prune_old_jobs

    days = load_search_profile(search_profile).max_age_days
    if days is None:
        typer.echo("No age limit set (max_age_days is empty); nothing to prune.")
        return
    c = cfg()
    conn = connect(c.db_path)
    n = prune_old_jobs(conn, days, dry_run=True)
    if dry_run or not n:
        typer.echo(f"{n} stored jobs are older than {days} days" + (" (dry run)." if n else "."))
        return
    backup = c.data_dir / "backups" / f"jobagent-{datetime.now():%Y%m%d-%H%M%S}.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    conn.close()
    shutil.copy2(c.db_path, backup)
    conn = connect(c.db_path)
    prune_old_jobs(conn, days)
    left = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    typer.echo(f"Removed {n} jobs older than {days} days; {left} remain. Backup: {backup}")


# --------------------------------------------------------------------------- routing (Stage 3)

ai_app = typer.Typer(help="AI provider checks.")
app.add_typer(ai_app, name="ai")


def _runner(conn, provider_name: str | None, max_calls: int | None = None):
    from jobagent.ai.provider import AIRunner
    from jobagent.ai.providers import build_provider
    from jobagent.routing.ai_router import PROMPT_VERSION
    from jobagent.routing.fake import fake_dispatch

    c = cfg()
    provider = build_provider(c.ai, provider_name, fake_fallback=fake_dispatch)
    return AIRunner(provider, conn, max_calls or c.ai.max_calls_per_run, PROMPT_VERSION)


@ai_app.command("ping")
def ai_ping(
    provider: str = typer.Option(None, help="claude_code | ollama | anthropic_api | fake"),
) -> None:
    """One tiny structured call to check the provider works."""
    from pydantic import BaseModel

    from jobagent.ai.provider import AIError
    from jobagent.db import connect

    class Pong(BaseModel):
        verdict: str

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict"],
        "properties": {"verdict": {"type": "string", "enum": ["KENYA", "OTHER"]}},
    }
    conn = connect(cfg().db_path)
    runner = _runner(conn, provider, max_calls=2)
    try:
        out, res = runner.call(
            "ping",
            "Classify job location text. Reply with JSON only.",
            "Location: 'Remote, open to candidates in Kenya'",
            schema,
            Pong,
        )
    except AIError as e:
        raise typer.BadParameter(f"{runner.provider.name}: {e}") from e
    typer.echo(
        f"{res.provider} / {res.model}: {out.verdict} in {res.latency_ms} ms "
        f"(tokens in {res.input_tokens}, out {res.output_tokens})"
    )


@app.command()
def route(
    limit: int = typer.Option(None, help="Route at most this many undecided jobs"),
    provider: str = typer.Option(None, help="Override config ai.provider"),
    rules_only: bool = typer.Option(False, help="Free rules only; leave the rest undecided"),
    no_resolve: bool = typer.Option(False, help="Do not trace discovery jobs to employers"),
    redo: bool = typer.Option(False, help="Re-route jobs that already have a decision"),
    job: list[str] = typer.Option(None, "--job", help="Route (or re-route) only these job ids"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Decide and show, store nothing"),
    search_profile: Path = typer.Option(Path("config/search_profile.yaml")),
) -> None:
    """Stage 3: route undecided jobs to APPLY, REVIEW, SKIP, CHECK_ELIGIBILITY or LINKEDIN_LEAD."""
    from jobagent.db import connect
    from jobagent.routing.router import route_jobs
    from jobagent.sourcing.config import load_search_profile

    c = cfg()
    prof = load_search_profile(search_profile)
    conn = connect(c.db_path)
    client = None if no_resolve else _client()
    try:
        rep = route_jobs(
            conn,
            prof,
            c.routing,
            None if rules_only else _runner(None if dry_run else conn, provider),
            resolver=None if client is None else _resolver(conn, client),
            batch_size=c.ai.batch_size,
            limit=limit,
            redo=redo,
            dry_run=dry_run,
            job_ids=job or None,
        )
    finally:
        if client:
            client.close()
    typer.echo(
        ("DRY RUN: nothing stored\n" if dry_run else "")
        + f"{rep.considered} jobs considered, {len(rep.decisions)} decided, "
        f"{rep.pending} left for the AI, {rep.resolved} traced to the employer, "
        f"{rep.ai_calls} AI calls"
    )
    typer.echo("Routes: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.by_route.items())))
    typer.echo("Decided by: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.by_stage.items())))
    if rep.paused:
        typer.echo(f"AI paused: {rep.paused}. Undecided jobs will be routed next run.")
    _print_decisions(rep.decisions)


def _print_decisions(decisions: list[dict], show_skips: bool = True) -> None:
    order = {"APPLY": 0, "CHECK_ELIGIBILITY": 1, "REVIEW": 2, "LINKEDIN_LEAD": 3, "SKIP": 4}
    for d in sorted(decisions, key=lambda d: (order.get(d["route"], 9), -(d.get("priority") or 0))):
        if d["route"] == "SKIP" and not show_skips:
            continue
        typer.echo(
            f"\n[{d['route']}] {d['title']} @ {d['company']}  (priority {d.get('priority') or 0}, "
            f"{d['stage']})"
        )
        if d.get("eligibility_verdict") or d.get("quoted_location_text"):
            typer.echo(
                f"  eligibility: {d.get('eligibility_verdict') or '-'}  quote: "
                f'"{(d.get("quoted_location_text") or "")[:140]}"'
            )
        hr = d.get("hours_requirement")
        if isinstance(hr, str):
            hr = json.loads(hr)
        if hr:
            us = ", US hours required" if hr.get("us_hours_required") else ""
            typer.echo(
                f"  hours: {hr.get('employment')}{us} {hr.get('timezone_note') or ''}".rstrip()
            )
        reasons = d["reasons"] if isinstance(d["reasons"], list) else json.loads(d["reasons"])
        for r in reasons[:4]:
            typer.echo(f"  - {r}")


@app.command()
def decisions(
    route_filter: str = typer.Option(None, "--route", help="Only this route"),
    limit: int = typer.Option(40),
    show_skips: bool = typer.Option(False, help="Include SKIP decisions"),
) -> None:
    """Show the current decision for each job, best first."""
    from jobagent.db import connect

    conn = connect(cfg().db_path)
    sql = (
        "SELECT d.*, j.title, j.company FROM decisions d JOIN jobs j ON j.id = d.job_id "
        "WHERE d.id = (SELECT MAX(id) FROM decisions WHERE job_id = d.job_id)"
    )
    args: list = []
    if route_filter:
        sql += " AND d.route = ?"
        args.append(route_filter.upper())
    rows = [
        dict(r) for r in conn.execute(sql + " ORDER BY d.priority DESC LIMIT ?", [*args, limit])
    ]
    counts = conn.execute(
        "SELECT route, COUNT(*) FROM decisions d WHERE d.id = (SELECT MAX(id) FROM decisions "
        "WHERE job_id = d.job_id) GROUP BY route"
    ).fetchall()
    typer.echo("Current routes: " + ", ".join(f"{r} {n}" for r, n in counts))
    _print_decisions(rows, show_skips=show_skips or bool(route_filter))


@app.command("eval")
def eval_cmd(
    provider: str = typer.Option(None, help="claude_code | ollama | anthropic_api | fake"),
    repeat: int = typer.Option(1, help="Run the set N times to measure route stability"),
    include_unconfirmed: bool = typer.Option(
        False, help="Also score labels the owner has not confirmed yet (marked in the report)"
    ),
    search_profile: Path = typer.Option(Path("config/search_profile.yaml")),
) -> None:
    """Score routing against the labelled eval set (data/<profile>/evals)."""
    from jobagent.evals.dataset import load_labels, load_postings
    from jobagent.evals.runner import report_markdown, run_eval, save_report
    from jobagent.sourcing.config import load_search_profile

    c = cfg()
    d = c.data_dir / "evals"
    labels = load_labels(d / "labels.yaml")
    confirmed = sum(lab.confirmed for lab in labels.values())
    if not confirmed and not include_unconfirmed:
        raise typer.BadParameter(
            f"none of the {len(labels)} labels in {d / 'labels.yaml'} is confirmed yet; "
            "confirm them first or pass --include-unconfirmed"
        )
    res = run_eval(
        load_postings(d / "postings.jsonl"),
        labels,
        load_search_profile(search_profile),
        c.routing,
        lambda conn: _runner(conn, provider),
        batch_size=c.ai.batch_size,
        repeats=repeat,
        include_unconfirmed=include_unconfirmed,
    )
    title = f"Routing eval ({c.profile} set)"
    path = save_report(res, d / "results", title)
    typer.echo(report_markdown(res, title))
    typer.echo(f"Report saved: {path}")
