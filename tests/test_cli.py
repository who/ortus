"""Smoke test that the CLI module imports and exposes the typer app."""

import importlib
from pathlib import Path

from typer.testing import CliRunner

from ortus.cli import app

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
DOCS = REPO / "docs"


def _page(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


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


def test_top_level_help_is_backend_neutral() -> None:
    """The entry point must not read as Claude-only; grind is backend-neutral."""

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    # Typer wraps the help into a box, so compare on collapsed whitespace
    # rather than on the exact line breaks the terminal width produced.
    rendered = " ".join(result.stdout.split())
    assert "Claude Code workflows" not in rendered
    for backend in ("Claude", "Codex", "Grok", "opencode"):
        assert backend in rendered


def test_prompt_verbs_are_documented() -> None:
    assert "ortus prompt" in _page("commands.md")
    page = _page("prompts.md")
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
        assert needle in page


def test_init_managed_agent_files_are_documented() -> None:
    backends = _page("backends.md")
    for needle in (
        "--backend all",
        "CLAUDE.md",
        "block=agents",
        "block=pointer",
        "AGENTS.override.md",
        "provisioned but not runnable",
        "preserved byte-for-byte",
        "`ortus init --force`",
        'pins `backend = "claude"`',
    ):
        assert needle in backends
    assert '"all" is init-only and invalid here' in _page("configuration.md")


def test_grok_backend_is_documented() -> None:
    page = _page("backends.md")
    lowered = page.lower()
    assert "grok" in lowered
    assert "claude remains the default" in lowered
    assert "grok -p" in page
    assert "/goal" in page


def test_prototype_verification_is_documented() -> None:
    config = _page("configuration.md")
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
    text = README.read_text(encoding="utf-8")
    quick_start = text[text.index("## Quick start") : text.index("## Prerequisites")]
    assert "ortus grind . --prototype" in quick_start
