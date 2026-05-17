"""Tests for ortus plan (xvel.5 acceptance criteria)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import plan as plan_mod
from ortus.core.claude import ClaudeRunner
from tests._shims import shim_path

pytestmark = pytest.mark.integration
runner = CliRunner()

FAKE_CLAUDE_PLAN = shim_path("fake-claude-plan")
TINY_PRD = Path(__file__).parent / "fixtures" / "sample-prds" / "tiny-3-task.md"


@pytest.fixture()
def bd_workspace(tmp_path: Path) -> Path:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    subprocess.run(
        ["bd", "init", "--prefix", "fixture"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    return tmp_path


def _swap_runner(monkeypatch: pytest.MonkeyPatch, binary: str) -> None:
    monkeypatch.setattr(plan_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=binary))


def test_plan_with_prd_creates_issues_in_repo(
    bd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #1 + #2: PRD decomposition creates N issues in <repo>/.beads/."""
    _swap_runner(monkeypatch, str(FAKE_CLAUDE_PLAN))

    # Place the PRD outside the workspace to verify FR-014 (no cd-to-PRD bug).
    prd_elsewhere = bd_workspace.parent / "elsewhere" / "prd.md"
    prd_elsewhere.parent.mkdir(parents=True)
    prd_elsewhere.write_text(TINY_PRD.read_text())

    result = runner.invoke(app, ["plan", str(bd_workspace), str(prd_elsewhere)])
    assert result.exit_code == 0, result.stdout + result.stderr

    # 3 issues land in the workspace's .beads/, NOT in the PRD's directory.
    assert not (prd_elsewhere.parent / ".beads").exists(), \
        "FR-014 bug regression: issues created in PRD's directory"

    bd_list = subprocess.run(
        ["bd", "ready", "--json"],
        cwd=str(bd_workspace),
        capture_output=True,
        text=True,
        check=True,
    )
    import json

    ready = json.loads(bd_list.stdout)
    assert len(ready) == 3, f"expected 3 ready issues, got {len(ready)}"


def test_plan_summary_lists_each_new_id(
    bd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: printed summary lists each created id."""
    _swap_runner(monkeypatch, str(FAKE_CLAUDE_PLAN))
    result = runner.invoke(app, ["plan", str(bd_workspace), str(TINY_PRD)])
    assert result.exit_code == 0
    # Summary includes 3 ids; each should match the prefix-id pattern.
    assert "plan created 3 issue(s)" in result.stdout
    assert result.stdout.count("fixture-") >= 3


def test_plan_no_prd_runs_idea_expansion(
    bd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #4: no-args plan runs interactive idea flow."""
    # Use the same fake-claude-plan which creates 3 dummy issues — proves the
    # no-args path also reaches the claude runner.
    _swap_runner(monkeypatch, str(FAKE_CLAUDE_PLAN))
    result = runner.invoke(app, ["plan", str(bd_workspace)])
    assert result.exit_code == 0
    assert "plan created" in result.stdout


def test_plan_missing_prd_exits_one(bd_workspace: Path) -> None:
    result = runner.invoke(app, ["plan", str(bd_workspace), "/nonexistent/path.md"])
    assert result.exit_code == 1
    assert "PRD not found" in result.stderr or "PRD not found" in result.stdout


def test_plan_fr003_no_beads(tmp_path: Path) -> None:
    """FR-003 still enforced by plan."""
    bogus = tmp_path / "no-beads"
    bogus.mkdir()
    result = runner.invoke(app, ["plan", str(bogus)])
    assert result.exit_code == 1
