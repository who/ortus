"""Tests for ortus interview (idzn.1 acceptance criteria)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import interview as iv
from ortus.core.claude import ClaudeRunner
from tests._shims import shim_path

pytestmark = pytest.mark.integration
runner = CliRunner()

FAKE = shim_path("fake-claude-interview")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    subprocess.run(
        ["bd", "init", "--prefix", "iv"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


def _created_at(workspace: Path, issue_id: str) -> str:
    out = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    if isinstance(data, list):
        data = data[0]
    return data["created_at"]


def _create_feature(workspace: Path, title: str, *, after: str | None = None) -> str:
    """Create an open feature; if `after` is given, retry until bd stamps it
    strictly later than that issue so the created_at ordering is real."""
    deadline = time.monotonic() + 10
    while True:
        new_id = subprocess.run(
            ["bd", "create", "--silent", "--title", title,
             "--type", "feature", "--priority", "2"],
            cwd=str(workspace), check=True, capture_output=True, text=True,
        ).stdout.strip()
        if after is None or _created_at(workspace, after) < _created_at(workspace, new_id):
            return new_id
        # Same second as `after`: drop this one and try again once bd's clock moves.
        subprocess.run(
            ["bd", "close", new_id], cwd=str(workspace), check=True, capture_output=True
        )
        assert time.monotonic() < deadline, "bd created_at never advanced"
        time.sleep(0.25)


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
    log = (workspace / "logs" / "interview.log").read_text(encoding="utf-8")
    assert feature_id in log, "feature_id should be substituted into the prompt"


def test_interview_picks_first_open_feature_when_no_id(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #1: no feature_id → picks first open feature."""
    f1 = _create_feature(workspace, "first")
    # bd stores `created_at` at second resolution, so both creates can land
    # in the same second and tie _pick_feature's sort. Wait on bd's own
    # observed timestamp rather than a fixed sleep: the assertion below is
    # only meaningful once the two features are actually ordered.
    f2 = _create_feature(workspace, "second", after=f1)
    assert _created_at(workspace, f1) < _created_at(workspace, f2)
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["interview", str(workspace)])
    assert result.exit_code == 0, result.stdout + result.stderr
    log = (workspace / "logs" / "interview.log").read_text(encoding="utf-8")
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
    assert "Interview started" in (workspace / "logs" / "interview.log").read_text(encoding="utf-8")
