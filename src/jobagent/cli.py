"""Command-line entry point: `jobagent`."""

from __future__ import annotations

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
    try:
        reports = run_sourcing(
            conn,
            load_sources(sources_file),
            load_search_profile(search_profile),
            client,
            only=only or None,
            dry_run=dry_run,
            resolve=not no_resolve,
            max_detail_fetches=c.sourcing.max_detail_fetches_per_source,
            max_new_companies=c.sourcing.max_new_companies_per_run,
        )
    finally:
        client.close()
    head = (
        f"{'source':24} {'status':9} {'listed':>6} {'title-':>6} {'dupes':>5} "
        f"{'new':>4} {'resolv':>6} {'reqs':>4}"
    )
    typer.echo(("DRY RUN: nothing written\n" if dry_run else "") + head + "\n" + "-" * len(head))
    for r in reports:
        typer.echo(
            f"{r.name[:24]:24} {r.status:9} {r.listed:>6} {r.title_filtered:>6} "
            f"{r.duplicates:>5} {r.new:>4} {r.resolved:>6} {r.requests:>4}"
        )
    for r in reports:
        for line in ([f"error: {r.error}"] if r.error else []) + [n for n in r.notes if n]:
            typer.echo(f"  {r.name}: {line}")
    typer.echo(f"\nTotal new: {sum(r.new for r in reports)}   database: {c.db_path}")
    for r in reports:
        if r.sample:
            typer.echo(f"\n{r.name}:\n  " + "\n  ".join(r.sample))


@app.command()
def add(
    url: str = typer.Argument(None, help="Posting URL (fetched only if it is a supported ATS)"),
    text_file: Path = typer.Option(None, exists=True, dir_okay=False, help="Pasted posting text"),
    text: str = typer.Option(None, help="Pasted posting text (short)"),
    title: str = typer.Option(None),
    company: str = typer.Option(None),
    employer_text: bool = typer.Option(
        False, help="The pasted text is copied from the employer's own posting"
    ),
) -> None:
    """Add a job found anywhere (X, WhatsApp, LinkedIn, hiring.cafe). Source = manual."""
    from jobagent.db import connect
    from jobagent.sourcing.pipeline import ManualIntakeError, add_manual

    body = text_file.read_text(encoding="utf-8") if text_file else text
    conn = connect(cfg().db_path)
    client = _client()
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
