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


def test_readme_documents_prompt_verbs() -> None:
    text = README.read_text(encoding="utf-8")
    verbs = text[text.index("## The verbs") : text.index("## Prerequisites")]
    assert "ortus prompt" in verbs
    section = text[text.index("## Runtime prompts") : text.index("## Glossary")]
    for needle in (
        "ortus prompt list",
        "ortus prompt show",
        "ortus prompt eject",
        "--origin",
        "--user",
        "--force",
        "`<repo>/.ortus/prompts/<name>.md`",
        "`~/.ortus/prompts/<name>.md`",
    ):
        assert needle in section


def test_readme_documents_init_managed_agent_files() -> None:
    text = README.read_text(encoding="utf-8")
    for needle in (
        "--backend all",
        "CLAUDE.md",
        "block=agents",
        "block=pointer",
        "AGENTS.override.md",
        "provisioned but not runnable",
        "preserved byte-for-byte",
        "`ortus init --force`",
    ):
        assert needle in text
    backends = text[text.index("## Agent backends") : text.index("## Why ortus")]
    assert 'pins `backend = "claude"`' in backends
    config = text[text.index("## Configuration") : text.index("## Runtime prompts")]
    assert '"all" is init-only and invalid here' in config


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


def test_readme_documents_prototype_verification() -> None:
    text = README.read_text(encoding="utf-8")
    config = text[text.index("## Configuration") : text.index("## Runtime prompts")]
    for needle in (
        'verification = "full"   # full | prototype (default: full)',
        "`ortus grind --prototype`",
        "criterion-check commands",
        "linter",
        "syntax or compile gate",
        "behavioral test\ncommands and the repo test suite",
        "lowered\nbar",
    ):
        assert needle in config
    quick_start = text[text.index("## Quick start") : text.index("## The verbs")]
    assert "ortus grind . --prototype" in quick_start
