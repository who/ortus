"""End-to-end smoke tests for grind/plan against seeded fixtures (xvel.6).

These exercise the claude_mock fixture and the seeded-3-issues fixture
together, replaying canned stream-json captures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.commands import plan as plan_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.sandbox import SandboxInfo

pytestmark = [pytest.mark.integration, pytest.mark.smoke, pytest.mark.slow]
runner = CliRunner()


def _stub_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def test_seeded_3_issues_has_expected_graph(seeded_3_issues: Path) -> None:
    """Acceptance #1: seeded-3-issues fixture has the expected 3-issue graph."""
    proc = subprocess.run(
        ["bd", "list", "--status=open", "--json"],
        cwd=str(seeded_3_issues),
        check=True,
        capture_output=True,
        text=True,
    )
    issues = json.loads(proc.stdout)
    assert len(issues) == 3
    types = sorted(i["issue_type"] for i in issues)
    assert types == ["epic", "task", "task"]


def test_seeded_3_issues_has_one_ready(seeded_3_issues: Path) -> None:
    """Of the 3 seeded, only the ready child should be in bd ready."""
    proc = subprocess.run(
        ["bd", "ready", "--json"],
        cwd=str(seeded_3_issues),
        check=True,
        capture_output=True,
        text=True,
    )
    ready = json.loads(proc.stdout)
    # The epic and ready child are both "ready" in beads terms (epic has no
    # dependencies); the blocked child waits on the ready one.
    titles = sorted(i["title"] for i in ready)
    assert "Child ready" in titles
    assert "Child blocked" not in titles


def test_claude_mock_resolves_all_three_scenarios(
    claude_mock: Callable[[str], Path],
) -> None:
    """Acceptance #2: at least 3 named scenarios available."""
    for scenario in ("grind-empty-queue", "grind-one-complete", "grind-blocked"):
        path = claude_mock(scenario)
        assert path.is_file()
        if sys.platform == "win32":
            # Windows: shim_path() returns a generated .bat wrapper that
            # CreateProcess can execute by extension; POSIX +x bit is
            # meaningless here. Validate via the extension and os.access.
            assert path.suffix.lower() == ".bat", (
                f"{scenario} should resolve to a .bat wrapper on Windows; got {path}"
            )
            assert os.access(path, os.X_OK), f"{scenario} bat should be readable/executable"
        else:
            assert path.stat().st_mode & 0o111, f"{scenario} should be executable"


def test_claude_mock_missing_scenario_raises(
    claude_mock: Callable[[str], Path],
) -> None:
    with pytest.raises(FileNotFoundError, match="no canned claude scenario"):
        claude_mock("no-such-scenario")


def test_grind_with_canned_grind_one_complete_closes_one_issue(
    seeded_3_issues: Path,
    claude_mock: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance #4 + spirit of #1: grind --tasks 1 against the seeded
    fixture closes exactly one issue end-to-end with the canned response."""
    shim = claude_mock("grind-one-complete")
    _stub_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: seeded_3_issues.parent / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )
    # Make sure settings.json exists so check_hooks_enabled has something to read.
    settings = seeded_3_issues / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

    open_before = json.loads(
        subprocess.run(
            ["bd", "list", "--status=open", "--json"],
            cwd=str(seeded_3_issues),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )

    # --tasks 1 caps at exactly one bd-state-verified close (the canned
    # grind-one-complete shim closes one issue per spawn; without the cap
    # the outer loop would continue and drain the whole 3-issue fixture).
    result = runner.invoke(
        app,
        ["grind", str(seeded_3_issues), "--tasks", "1", "--idle-sleep", "0"],
    )
    log_files = list((seeded_3_issues / "logs").glob("grind-*.log"))
    log_dump = "\n".join(p.read_text(encoding="utf-8") for p in log_files) if log_files else "(no log)"
    assert result.exit_code == 0, (
        f"rc={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        f"\n---LOG---\n{log_dump}"
    )

    open_after = json.loads(
        subprocess.run(
            ["bd", "list", "--status=open", "--json"],
            cwd=str(seeded_3_issues),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert len(open_after) == len(open_before) - 1, (
        f"expected exactly 1 issue closed; before={len(open_before)} after={len(open_after)}"
    )


def test_plan_with_canned_response_creates_issues(
    seeded_3_issues: Path,
    claude_mock: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror smoke: plan against tiny-3-task with the existing fake-claude-plan
    shim (also lives as a scenario-style fixture)."""
    from tests.test_plan import FAKE_CLAUDE_PLAN, TINY_PRD

    monkeypatch.setattr(
        plan_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(FAKE_CLAUDE_PLAN))
    )
    result = runner.invoke(app, ["plan", str(seeded_3_issues), str(TINY_PRD)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "plan created 3 issue(s)" in result.stdout
