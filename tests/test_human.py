"""Tests for ortus human (idzn.3 acceptance criteria)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.human import _render_options

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    subprocess.run(
        ["bd", "init", "--prefix", "hu"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


def _make_human_flagged(workspace: Path, title: str, comment: str | None = None) -> str:
    issue_id = subprocess.run(
        [
            "bd", "create", "--silent", "--title", title, "--type", "task",
            "--priority", "2", "--labels", "human",
        ],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    ).stdout.strip()
    if comment:
        subprocess.run(
            ["bd", "comment", issue_id, comment],
            cwd=str(workspace), check=True, capture_output=True,
        )
    return issue_id


def test_no_human_issues_emits_empty_report(workspace: Path) -> None:
    result = runner.invoke(app, ["human", str(workspace)])
    assert result.exit_code == 0
    report = (workspace / "HUMAN-TODO.md").read_text()
    assert "No issues are currently flagged" in report


def test_human_report_lists_flagged_issues(workspace: Path) -> None:
    """Acceptance #1: report includes each flagged issue."""
    a = _make_human_flagged(workspace, "needs decision A")
    b = _make_human_flagged(workspace, "needs decision B", comment="why blocked")
    result = runner.invoke(app, ["human", str(workspace)])
    assert result.exit_code == 0
    report = (workspace / "HUMAN-TODO.md").read_text()
    assert a in report
    assert b in report
    assert "needs decision A" in report
    assert "needs decision B" in report
    assert "why blocked" in report


def test_human_report_renders_pros_cons_when_markers_present() -> None:
    """Acceptance #2: option markers in comment → structured-options section."""
    text = (
        "We have to pick one.\n"
        "**Option 1:** Roll back to v1.\n"
        "**Option 2:** Patch v2 and continue.\n"
        "**Option 3:** Build v3 from scratch.\n"
    )
    rendered = _render_options(text)
    assert rendered is not None
    assert "Structured options:" in rendered
    assert "Option 1" in rendered
    assert "Option 2" in rendered
    assert "Option 3" in rendered


def test_render_options_returns_none_for_no_markers() -> None:
    assert _render_options("just plain text\n") is None


def test_human_nfr006_no_writes_to_beads_payload(workspace: Path) -> None:
    """NFR-006 — ortus human must not change any issue-relevant bd state.

    bd uses Dolt as a backing store, which touches mtimes on its internal
    journal/manifest files even for read-only queries. The NFR's intent is
    that no semantic state change happens: issue payloads, comments, deps
    are stable across an `ortus human` invocation. We verify by comparing
    `bd list` JSON before/after, which is the actual data surface.
    """
    issue_id = _make_human_flagged(workspace, "x", comment="some comment")
    before = subprocess.run(
        ["bd", "list", "--status", "open", "--json"],
        cwd=str(workspace), capture_output=True, text=True, check=True,
    ).stdout
    before_comments = subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=str(workspace), capture_output=True, text=True, check=True,
    ).stdout

    runner.invoke(app, ["human", str(workspace)])

    after = subprocess.run(
        ["bd", "list", "--status", "open", "--json"],
        cwd=str(workspace), capture_output=True, text=True, check=True,
    ).stdout
    after_comments = subprocess.run(
        ["bd", "comments", issue_id, "--json"],
        cwd=str(workspace), capture_output=True, text=True, check=True,
    ).stdout
    assert before == after, "ortus human must not change bd issue payloads"
    assert before_comments == after_comments, "ortus human must not change comments"


def test_human_only_writes_human_todo_md(workspace: Path) -> None:
    """The only file ortus human creates outside .beads/dolt internals is HUMAN-TODO.md."""
    _make_human_flagged(workspace, "x")
    # Snapshot what exists OUTSIDE .beads/ (bd's internal mtimes are out of scope).
    def _snap_root() -> set[str]:
        out: set[str] = set()
        for entry in workspace.iterdir():
            if entry.name == ".beads":
                continue
            out.add(entry.name)
        return out

    before = _snap_root()
    runner.invoke(app, ["human", str(workspace)])
    after = _snap_root()
    new = after - before
    assert new == {"HUMAN-TODO.md"}, f"unexpected new files: {new}"


def test_human_todo_in_gitignore_template() -> None:
    """Acceptance #4: HUMAN-TODO.md is listed in the bundled .gitignore template."""
    from importlib.resources import files

    text = files("ortus.templates").joinpath(".gitignore.jinja").read_text()
    assert "HUMAN-TODO.md" in text


def test_human_no_file_prints_without_writing(workspace: Path) -> None:
    _make_human_flagged(workspace, "x")
    result = runner.invoke(app, ["human", str(workspace), "--no-file"])
    assert result.exit_code == 0
    assert "Human-decision queue" in result.stdout
    assert not (workspace / "HUMAN-TODO.md").exists()


@pytest.mark.smoke
def test_human_smoke_end_to_end(workspace: Path) -> None:
    """Smoke: full end-to-end with 2 human-flagged issues + structured options."""
    a = _make_human_flagged(workspace, "smoke A", comment="**Option 1:** ship\n**Option 2:** revert")
    b = _make_human_flagged(workspace, "smoke B")
    result = runner.invoke(app, ["human", str(workspace)])
    assert result.exit_code == 0
    report = (workspace / "HUMAN-TODO.md").read_text()
    assert a in report
    assert b in report
    assert "Structured options:" in report
    assert "Option 1" in report
