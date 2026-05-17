"""Tests for ortus interview (idzn.1 acceptance criteria)."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import interview as iv
from ortus.core.claude import ClaudeRunner

pytestmark = pytest.mark.integration
runner = CliRunner()

FAKE = Path(__file__).parent / "fixtures" / "bin" / "fake-claude-interview"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    subprocess.run(
        ["bd", "init", "--prefix", "iv"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


def _swap_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(iv, "_make_runner", lambda: ClaudeRunner(claude_binary=str(FAKE)))


def test_interview_jumps_to_supplied_feature_id(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #2: explicit feature_id is used as-is."""
    feature_id = subprocess.run(
        ["bd", "create", "--silent", "--title", "f-1", "--type", "feature", "--priority", "2"],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    ).stdout.strip()
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["interview", str(workspace), feature_id])
    assert result.exit_code == 0, result.stdout + result.stderr
    log = (workspace / "logs" / "interview.log").read_text()
    assert feature_id in log, "feature_id should be substituted into the prompt"


def test_interview_picks_first_open_feature_when_no_id(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #1: no feature_id → picks first open feature."""
    f1 = subprocess.run(
        ["bd", "create", "--silent", "--title", "first", "--type", "feature", "--priority", "2"],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    ).stdout.strip()
    # bd stores `created_at` at second resolution. Without this sleep, both
    # creates land in the same second on fast CI runners; _pick_feature's
    # sort by created_at then ties and either feature may be returned,
    # making the "first" assertion non-deterministic.
    time.sleep(1.1)
    subprocess.run(
        ["bd", "create", "--silent", "--title", "second", "--type", "feature", "--priority", "2"],
        cwd=str(workspace), check=True, capture_output=True,
    )
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["interview", str(workspace)])
    assert result.exit_code == 0, result.stdout + result.stderr
    log = (workspace / "logs" / "interview.log").read_text()
    assert f1 in log


def test_interview_exits_one_when_no_open_features(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: no open features → clear exit-1 instead of hanging."""
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["interview", str(workspace)])
    assert result.exit_code == 1
    assert "no open features" in (result.stdout + result.stderr)


def test_interview_prompt_bundled_and_resolvable(workspace: Path) -> None:
    """Acceptance #4: bundled interview-prompt.md resolves via 3-layer lookup."""
    from ortus.core.prompts import resolve_prompt

    res = resolve_prompt("interview-prompt", repo=workspace)
    assert res.source == "bundled"
    assert "Feature Interview Prompt" in res.text


def test_interview_warns_when_id_is_not_feature_type(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = subprocess.run(
        ["bd", "create", "--silent", "--title", "atask", "--type", "task", "--priority", "2"],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    ).stdout.strip()
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["interview", str(workspace), task_id])
    assert result.exit_code == 0
    # The verb runs but emits a warn line first.
    assert "is type='task'" in (result.stdout + result.stderr) or "not 'feature'" in (
        result.stdout + result.stderr
    )


@pytest.mark.smoke
def test_interview_smoke_with_canned_response(
    workspace: Path, claude_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: interview against a canned claude scenario; full path exercised."""
    subprocess.run(
        ["bd", "create", "--silent", "--title", "smoke feature", "--type", "feature", "--priority", "2"],
        cwd=str(workspace), check=True, capture_output=True,
    )
    shim = claude_mock("interview-pick-feature")
    monkeypatch.setattr(iv, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim)))
    result = runner.invoke(app, ["interview", str(workspace)])
    assert result.exit_code == 0
    assert "Interview started" in (workspace / "logs" / "interview.log").read_text()
