"""Tests for --package-manager / --framework / --linter on `ortus init`.

Covers default resolution per language, explicit overrides, and invalid
combinations. Run targeted: `uv run pytest tests/test_init_flags.py`.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.init_render import (
    FRAMEWORK_DEFAULTS,
    LINTER_DEFAULTS,
    PACKAGE_MANAGER_DEFAULTS,
    RenderContext,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture(autouse=True)
def _require_bd() -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd binary not on PATH")


@pytest.fixture(autouse=True)
def _fake_codegraph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the CodeGraph CLI so stack-flag tests stay hermetic."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: "/usr/bin/codegraph")
    monkeypatch.setattr(
        init_mod,
        "_codegraph_index",
        lambda repo, **kwargs: (repo / ".codegraph").mkdir(parents=True, exist_ok=True),
    )


@pytest.fixture(autouse=True)
def _fake_backend_clis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every backend CLI is installed.

    The default `--backend all` summarizes CLI availability per backend and
    fails when the pinned run backend's CLI is absent, so an unfaked lookup
    would make these tests answer for the host's installs. Tests about a
    missing CLI re-patch `_backend_cli` themselves.
    """
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")


def _ortusrc(target: Path) -> dict:
    return tomllib.loads((target / ".ortusrc").read_text())


@pytest.mark.parametrize(
    "project_type,expected_pm,expected_lint",
    [
        ("python", "uv", "ruff"),
        ("typescript", "npm", "eslint"),
        ("go", "gomod", "golangci"),
        ("rust", "cargo", "clippy"),
        ("polyglot", "none", "none"),
    ],
)
def test_per_language_defaults(
    tmp_path: Path, project_type: str, expected_pm: str, expected_lint: str
) -> None:
    target = tmp_path / project_type
    result = runner.invoke(
        app, ["init", str(target), "--project-type", project_type]
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert PACKAGE_MANAGER_DEFAULTS[project_type] == expected_pm
    assert LINTER_DEFAULTS[project_type] == expected_lint
    assert FRAMEWORK_DEFAULTS[project_type] == "none"


def test_explicit_package_manager_override(tmp_path: Path) -> None:
    target = tmp_path / "py"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "python", "--package-manager", "pip"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")


def test_explicit_framework_override(tmp_path: Path) -> None:
    target = tmp_path / "py"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "python", "--framework", "fastapi"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")


def test_explicit_linter_override(tmp_path: Path) -> None:
    target = tmp_path / "ts"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "typescript", "--linter", "none"],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")


def test_invalid_package_manager_for_language(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "python", "--package-manager", "bun"],
    )
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--package-manager" in combined
    assert "'bun'" in combined or "bun" in combined
    # nothing should have been bootstrapped on a flag rejection
    assert not (target / ".beads").exists()


def test_invalid_framework_for_language(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "go", "--framework", "nextjs"],
    )
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--framework" in combined


def test_invalid_linter_for_language(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(
        app,
        ["init", str(target), "--project-type", "rust", "--linter", "ruff"],
    )
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--linter" in combined


def test_invalid_project_type_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad"
    result = runner.invoke(app, ["init", str(target), "--project-type", "cobol"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--project-type" in combined


def test_codegraph_cli_missing_fails_under_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: no CLI under the default policy fails before writing."""
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: None)
    target = tmp_path / "nocli"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    compact = "".join(combined.split())
    assert "codegraphCLIisnotonPATH" in compact, combined
    assert "--codegraphoff" in compact, combined
    # nothing bootstrapped on the rejection
    assert not (target / ".beads").exists()


def test_codegraph_off_skips_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: `off` never touches the CLI and pins itself."""
    import ortus.commands.init as init_mod

    def _never_lookup() -> str:
        raise AssertionError("--codegraph off must not look for the CodeGraph CLI")

    def _never_index(repo: Path, **kwargs: object) -> None:
        raise AssertionError("--codegraph off must not invoke the CodeGraph CLI")

    monkeypatch.setattr(init_mod, "_codegraph_cli", _never_lookup)
    monkeypatch.setattr(init_mod, "_codegraph_index", _never_index)
    target = tmp_path / "graphless"
    result = runner.invoke(app, ["init", str(target), "--codegraph", "off"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert _ortusrc(target)["codegraph"] == "off"
    assert not (target / ".codegraph").exists()


def test_invalid_codegraph_mode_rejected(tmp_path: Path) -> None:
    target = tmp_path / "badmode"
    result = runner.invoke(app, ["init", str(target), "--codegraph", "maybe"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--codegraph" in combined
    assert not (target / ".beads").exists()


def test_render_context_carries_new_fields() -> None:
    ctx = RenderContext(
        prefix="x",
        project_type="python",
        package_manager="uv",
        framework="fastapi",
        linter="ruff",
    )
    d = ctx.as_dict()
    assert d["package_manager"] == "uv"
    assert d["framework"] == "fastapi"
    assert d["linter"] == "ruff"


def test_help_advertises_new_flags() -> None:
    # NO_COLOR + TERM=dumb stop Rich from interleaving SGR escape sequences
    # through flag names in option tables, which otherwise break the literal
    # substring assertions below in CI's terminal.
    result = runner.invoke(
        app, ["init", "--help"], env={"NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0
    out = result.stdout
    assert "--package-manager" in out
    assert "--framework" in out
    assert "--linter" in out
    assert "claude|codex|grok" in out
    # the help text should defer per-language detail rather than enumerate it
    assert "depend on --project-type" in out
