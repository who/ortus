"""Regression tests for the one-issue Codex grind completion contract.

The outer loop is one worker per issue and that worker session-closes. A
composed prompt that still tells the model to leave candidate edits for a
later verification phase is a contract defect: grind must not spawn on it,
and a successful resume must land exactly once without a false no-close
window.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core.codegraph import (
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    LEAVE_OPEN_FOR_VERIFICATION,
    phase_contract,
)
from ortus.core.prompts import bundled_prompt_text
from tests.test_grind import (
    _bd_repo,
    _claim_with_no_close_marker,
    _CloseWithoutClaimsRunner,
    _comments_blob,
    _create_ready_issue,
    _fake_sandbox,
    _fixture_repo,
    _grind_log,
    _issue,
    _RecordingRunner,
    _squashed_console,
)

runner = CliRunner()


def _available_probe(mode: CodeGraphMode = CodeGraphMode.REQUIRED) -> CodeGraphProbe:
    return CodeGraphProbe(mode, True, True, True)


def _stock_codex_prompt() -> str:
    return grind_mod._compose_work_prompt(
        "",
        {"id": "repo-1", "title": "an issue"},
        "codex",
        phase_instruction=grind_mod._IMPLEMENTATION_INSTRUCTION,
        phase_contract_text=phase_contract(
            CodeGraphPhase.IMPLEMENTATION, _available_probe()
        ),
    )


def _write_goal_override(root: Path, text: str) -> Path:
    path = root / ".ortus" / "prompts" / "goal-prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ---------------------------------------------------------------------------
# prompt — stock Codex contract and override handling (AC-1)
# ---------------------------------------------------------------------------


def test_stock_codex_grind_prompt_requires_session_close() -> None:
    """The effective Codex prompt requires one-issue session-close."""
    body = _stock_codex_prompt()
    lowered = body.lower()
    assert not body.lstrip().startswith("/goal")
    assert "session-close" in lowered
    assert "ortus prompt show goal" in body
    assert "achieved when that issue is closed" in lowered
    assert "in sync with origin" in lowered
    assert "session-close is still this worker's job" in lowered


def test_stock_codex_grind_prompt_omits_leave_open_instruction() -> None:
    """Stock composition must not hand off to an unscheduled verification phase."""
    body = _stock_codex_prompt()
    assert LEAVE_OPEN_FOR_VERIFICATION not in body.lower()
    assert "do not close the issue" not in body.lower()
    contract = phase_contract(CodeGraphPhase.IMPLEMENTATION, _available_probe())
    assert LEAVE_OPEN_FOR_VERIFICATION not in contract.lower()
    assert "do not close the issue" not in contract.lower()
    assert "impact query" in contract.lower()


def test_prompt_diagnostic_silent_on_stock_layers(tmp_path: Path) -> None:
    """Bundled goal plus a clean composed prompt is not a conflict."""
    diagnostic = grind_mod._stale_completion_contract_diagnostic(
        _stock_codex_prompt(),
        repo=tmp_path,
        home=tmp_path / "home",
    )
    assert diagnostic is None


def test_prompt_diagnostic_names_repo_override_source(tmp_path: Path) -> None:
    """A stale repo override is reported with its path, not overwritten."""
    override = _write_goal_override(
        tmp_path,
        "Custom loop.\nDo not close the issue; leave candidate edits for verification.\n",
    )
    before = override.read_text(encoding="utf-8")
    diagnostic = grind_mod._stale_completion_contract_diagnostic(
        "Follow the one-issue goal-prompt loop. Session-close that id.",
        repo=tmp_path,
        home=tmp_path / "home",
    )
    assert diagnostic is not None
    assert "repo override" in diagnostic
    assert str(override) in diagnostic
    assert "ortus prompt eject goal --force" in diagnostic
    assert override.read_text(encoding="utf-8") == before


def test_compatible_prompt_override_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom goal prompt without the leave-open sentence still wins."""
    repo = _fixture_repo(tmp_path)
    extra = "Operator note: keep the one-issue session-close loop.\n"
    override = _write_goal_override(
        repo, bundled_prompt_text("goal-prompt") + extra
    )
    _isolate_home(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["grind", str(repo), "--backend", "codex", "--dry-run"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    prompt = result.stdout.split("--- per-iteration prompt ---", 1)[1]
    assert "session-close" in prompt.lower()
    assert LEAVE_OPEN_FOR_VERIFICATION not in prompt.lower()
    assert extra in override.read_text(encoding="utf-8")


def test_stale_leave_open_prompt_override_rejected_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run refuses a stale goal override and leaves the file untouched."""
    repo = _fixture_repo(tmp_path)
    override = _write_goal_override(
        repo,
        "Custom loop.\nDo not close the issue; leave candidate edits for verification.\n",
    )
    before = override.read_text(encoding="utf-8")
    _isolate_home(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["grind", str(repo), "--backend", "codex", "--dry-run"]
    )
    combined = " ".join((result.stdout + result.stderr).split())
    assert result.exit_code == 1, combined
    assert "stale completion contract" in combined
    assert "repo override" in combined
    assert "goal-prompt.md" in combined
    assert override.read_text(encoding="utf-8") == before


@pytest.mark.slow
@pytest.mark.integration
def test_stale_leave_open_prompt_override_does_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live grind must not spend a worker on a conflicting override."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "stale-override-spawn")
    _create_ready_issue(repo, "must not spawn")
    _write_goal_override(
        repo,
        "Custom loop.\nDo not close the issue; leave candidate edits for verification.\n",
    )
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 1, combined
    assert not recorded.calls
    assert "stale completion contract" in combined


# ---------------------------------------------------------------------------
# completion — a valid resume lands once (AC-2)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
def test_resumed_completion_lands_one_task_without_no_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover claim that session-closes counts as one land, not a stall."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "resume-complete")
    issue_id = _claim_with_no_close_marker(repo, "resume and close", windows=None)
    (repo / "candidate.py").write_text("x = 1\n", encoding="utf-8")
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    log = _grind_log(repo)
    assert f"worker closed {issue_id}" in log
    assert "no-close window" not in log
    console = _squashed_console(result)
    assert "1 landed this session" in console
    assert (repo / "candidate.py").read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.slow
@pytest.mark.integration
def test_fresh_completion_respects_task_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh close counts as one land and honours --tasks 1."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "fresh-cap")
    first = _create_ready_issue(repo, "first leaf")
    second = _create_ready_issue(repo, "second leaf")
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    statuses = {
        first: _issue(repo, first)["status"],
        second: _issue(repo, second)["status"],
    }
    assert set(statuses.values()) == {"closed", "open"}
    assert sum(1 for status in statuses.values() if status == "closed") == 1
    console = _squashed_console(result)
    assert "1 landed this session" in console
    log = _grind_log(repo)
    assert "--tasks cap reached: 1/1" in log


# ---------------------------------------------------------------------------
# failure — unfinished, blocked, and stalled work is not a land (AC-3)
# ---------------------------------------------------------------------------


class _FailedPushRunner:
    """Commits nothing, cannot push, leaves the claim open, exits normally."""

    extra_env: dict[str, str] = {}

    def __init__(self, host: Path) -> None:
        self.host = host

    def run(self, prompt: str, **kwargs: object) -> int:
        listing = json.loads(
            subprocess.run(
                ["bd", "list", "--status=in_progress", "--json"],
                cwd=self.host,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        if not listing:
            ready = json.loads(
                subprocess.run(
                    ["bd", "ready", "--json"],
                    cwd=self.host,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            issue_id = next(
                item["id"] for item in ready if item.get("issue_type") != "epic"
            )
            subprocess.run(
                ["bd", "update", issue_id, "--status=in_progress"],
                cwd=self.host,
                check=True,
                capture_output=True,
            )
        push = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=self.host,
            capture_output=True,
            text=True,
        )
        assert push.returncode != 0
        return 0


@pytest.mark.slow
@pytest.mark.integration
def test_failure_exit_without_close_is_not_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal worker exit that leaves the claim open is not a land."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "exit-open")
    issue_id = _claim_with_no_close_marker(repo, "stays open", windows=None)
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert recorded.calls
    assert _issue(repo, issue_id)["status"] == "in_progress"
    console = _squashed_console(result)
    assert "0 landed this session" in console
    assert f"closed {issue_id}" not in console


@pytest.mark.slow
@pytest.mark.integration
def test_failure_preserves_inherited_dirty_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrelated dirty files survive a no-close window."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "keep-dirty")
    issue_id = _claim_with_no_close_marker(repo, "keep dirt", windows=None)
    leftover = repo / "inherited.txt"
    leftover.write_text("operator scratch\n", encoding="utf-8")
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert leftover.read_text(encoding="utf-8") == "operator scratch\n"
    assert _issue(repo, issue_id)["status"] == "in_progress"
    assert "0 landed this session" in _squashed_console(result)


@pytest.mark.slow
@pytest.mark.integration
def test_failure_resumed_stall_records_no_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed window that neither closes nor advances HEAD burns one stall."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "stall-count")
    issue_id = _claim_with_no_close_marker(repo, "quiet stall", windows=None)
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "in_progress"
    assert "human" not in (_issue(repo, issue_id).get("labels") or [])
    assert "ortus-grind: no-close window 1" in _comments_blob(repo, issue_id)
    log = _grind_log(repo)
    assert "no-close window 1 of 2" in log
    assert "0 landed this session" in _squashed_console(result)


@pytest.mark.slow
@pytest.mark.integration
def test_failure_push_without_close_is_not_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that cannot push and does not close is not reported as landed."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "failed-push")
    issue_id = _claim_with_no_close_marker(repo, "cannot push", windows=None)
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _FailedPushRunner(repo)
    )
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "in_progress"
    assert "0 landed this session" in _squashed_console(result)
    assert f"worker closed {issue_id}" not in _grind_log(repo)


@pytest.mark.slow
@pytest.mark.integration
def test_failure_human_blocked_is_not_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover claim labelled human is the operator's, never a land."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = _bd_repo(tmp_path, "human-block")
    issue_id = _claim_with_no_close_marker(repo, "operator owned", windows=None)
    subprocess.run(
        ["bd", "label", "add", issue_id, "human"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    recorded = _RecordingRunner()
    _fake_sandbox(monkeypatch)
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: recorded)
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not recorded.calls
    shown = _issue(repo, issue_id)
    assert shown["status"] == "in_progress"
    assert "human" in (shown.get("labels") or [])
    console = _squashed_console(result)
    assert "queue already drained" in console
    assert f"closed {issue_id}" not in console
    assert "1 landed this session" not in console
