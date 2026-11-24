"""Command-line entry point: `jobagent`."""

import typer

from jobagent import __version__
from jobagent.logging_setup import setup_logging

app = typer.Typer(help="Find, route, score, draft and track job applications. Never submits.")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    setup_logging("DEBUG" if verbose else "INFO")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)
