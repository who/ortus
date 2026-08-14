"""Smoke test that the CLI module imports and exposes the typer app."""

import importlib
from pathlib import Path

from typer.testing import CliRunner

from ortus.cli import app

README = Path(__file__).resolve().parents[1] / "README.md"


def test_cli_imports() -> None:
    cli = importlib.import_module("ortus.cli")
    assert cli.app is not None


def test_main_module_imports() -> None:
    main = importlib.import_module("ortus.__main__")
    assert callable(main.main)


def test_package_version() -> None:
    ortus = importlib.import_module("ortus")
    assert ortus.__version__


def test_grind_help_lists_grok() -> None:
    result = CliRunner().invoke(app, ["grind", "--help"])
    assert result.exit_code == 0
    assert "grok" in result.stdout


def test_readme_lists_grok_backend() -> None:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Agent backends")
    end = text.index("## Why ortus", start)
    section = text[start:end]
    lowered = section.lower()
    assert "grok" in lowered
    assert "claude remains the default" in lowered
    assert "grok -p" in section
    assert "/goal" in section
