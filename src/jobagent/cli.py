"""Command-line entry point: `jobagent`."""

from __future__ import annotations

from pathlib import Path

import typer

from jobagent import __version__
from jobagent.logging_setup import setup_logging

app = typer.Typer(help="Find, route, score, draft and track job applications. Never submits.")
evidence_app = typer.Typer(help="Build and check the evidence bank.")
app.add_typer(evidence_app, name="evidence")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    setup_logging("DEBUG" if verbose else "INFO")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


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
