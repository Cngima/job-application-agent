"""Command-line entry point: `jobagent`."""

from __future__ import annotations

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


def _resolver(conn, client):
    from jobagent.sourcing.resolver import Resolver

    return Resolver(conn, client, max_new_companies=cfg().sourcing.max_new_companies_per_run)


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
