"""Tests for the `ortus spec` verb (ortus-xhrj.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.readiness import _REQUIRED_SECTIONS, spec_markdown

runner = CliRunner()


def test_spec_prints_contract_with_every_required_heading() -> None:
    result = runner.invoke(app, ["spec"])
    assert result.exit_code == 0, result.stdout + result.stderr
    for section in _REQUIRED_SECTIONS:
        assert section.heading in result.stdout, f"missing heading {section.heading!r}"


def test_spec_prints_contract_verbatim_on_stdout() -> None:
    """Rendered text reaches stdout unwrapped, so piping/redirecting is lossless."""
    result = runner.invoke(app, ["spec"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout == spec_markdown()


def test_spec_emits_progress_lines_on_stderr() -> None:
    result = runner.invoke(app, ["spec"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "[ortus spec]" in result.stderr
    assert "[ortus spec] done" in result.stderr


def test_spec_succeeds_without_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .beads/, no backend, no repo argument — the spec is repo-independent."""
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".beads").exists()
    result = runner.invoke(app, ["spec"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout == spec_markdown()


def test_spec_takes_no_arguments() -> None:
    result = runner.invoke(app, ["spec", str(Path.cwd())])
    assert result.exit_code != 0
