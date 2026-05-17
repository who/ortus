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
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "--package-manager" in out
    assert "--framework" in out
    assert "--linter" in out
    # the help text should defer per-language detail rather than enumerate it
    assert "depend on --project-type" in out
