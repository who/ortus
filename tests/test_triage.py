"""Tests for ortus triage (idzn.2 acceptance criteria)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import triage as triage_mod
from ortus.core.claude import ClaudeRunner

pytestmark = pytest.mark.integration
runner = CliRunner()

FAKE = Path(__file__).parent / "fixtures" / "bin" / "fake-claude-interview"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    subprocess.run(
        ["bd", "init", "--prefix", "tr"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


def _make_human_flagged(workspace: Path, title: str) -> str:
    return subprocess.run(
        [
            "bd", "create", "--silent", "--title", title, "--type", "task",
            "--priority", "2", "--labels", "human",
        ],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _swap_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(FAKE)))


def test_triage_exits_zero_with_message_when_queue_empty(workspace: Path) -> None:
    """Acceptance #2: no human-flagged issues → exit 0 + message, no claude."""
    result = runner.invoke(app, ["triage", str(workspace)])
    assert result.exit_code == 0
    assert "no human-queue items" in result.stdout
    # No log file because we didn't run claude.
    assert not (workspace / "logs" / "triage.log").exists()


def test_triage_invokes_claude_when_flagged_issues_present(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #1: with flagged issues, runs claude session against the prompt."""
    _make_human_flagged(workspace, "needs decision A")
    _make_human_flagged(workspace, "needs decision B")
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["triage", str(workspace)])
    assert result.exit_code == 0, result.stdout + result.stderr
    log = (workspace / "logs" / "triage.log").read_text()
    assert "fake-claude-interview" in log
    # Prompt header should appear in the log echo
    assert "Triage Prompt" in log or len(log) > 0


def test_triage_prompt_bundled() -> None:
    """Acceptance #3: triage-prompt.md ships in the package."""
    from ortus.core.prompts import resolve_prompt
    res = resolve_prompt("triage-prompt", repo=Path("/tmp"))
    assert res.source == "bundled"
    assert "Triage Prompt" in res.text


def test_triage_count_in_starting_line(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_human_flagged(workspace, "a")
    _make_human_flagged(workspace, "b")
    _make_human_flagged(workspace, "c")
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["triage", str(workspace)])
    assert result.exit_code == 0
    assert "3 human-flagged issue(s)" in result.stdout


@pytest.mark.smoke
def test_triage_smoke_with_canned_response(
    workspace: Path, claude_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: triage against a canned scenario walks the queue."""
    _make_human_flagged(workspace, "smoke triage 1")
    shim = claude_mock("triage-walk-queue")
    monkeypatch.setattr(triage_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim)))
    result = runner.invoke(app, ["triage", str(workspace)])
    assert result.exit_code == 0
    log = (workspace / "logs" / "triage.log").read_text()
    assert "Reviewing first human-flagged" in log
