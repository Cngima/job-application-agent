from pathlib import Path

from typer.testing import CliRunner

from jobagent import __version__
from jobagent.cli import app
from jobagent.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_version_command() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_loads() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.ai.max_calls_per_run == 60
    assert cfg.routing.evergreen_days == 60


def test_no_em_dashes_in_docs() -> None:
    for path in [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md")]:
        assert "—" not in path.read_text(encoding="utf-8"), f"em dash in {path.name}"
