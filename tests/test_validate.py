"""Tests for the `ortus validate` verb: the readiness verdict before a run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import validate as validate_mod
from ortus.core.readiness import validate_issue
from tests.conftest import copy_bd_workspace

pytestmark = pytest.mark.integration
runner = CliRunner()
README = Path(__file__).parent.parent / "README.md"


def _create_unready_issue(repo: Path, title: str) -> str:
    """A hand-authored leaf: real work, but no readiness schema v1 packet."""
    return subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            title,
            "--type",
            "task",
            "--priority",
            "2",
            "--description",
            "make it work",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _shown(repo: Path, issue_id: str) -> dict:
    """The issue as `bd show --json` returns it: what grind validates at claim."""
    listing = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(listing)[0]


def _rows(stdout: str) -> dict[str, str]:
    """stdout rows keyed by status word, one line each."""
    return {line.split(" ", 1)[0]: line for line in stdout.splitlines() if line}


def test_validate_ready_issue_exits_zero(tmp_path: Path) -> None:
    """AC-1: a leaf carrying a readiness schema v1 packet reads READY."""
    workspace = copy_bd_workspace(tmp_path / "repo", "leaf")
    repo, issue_id = workspace.path, workspace.issues[0]

    result = runner.invoke(app, ["validate", str(repo), issue_id])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [f"READY {issue_id}"]
    assert "done (1 ready" in result.stderr


def test_validate_unready_issue_matches_grind_diagnostic(tmp_path: Path) -> None:
    """AC-2: the row carries the exact diagnostic grind's claim-time validator
    produces for the same issue, and the verb exits 1."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    issue_id = _create_unready_issue(repo, "hand authored leaf")
    expected = validate_issue(_shown(repo, issue_id)).diagnostic()
    assert expected.startswith(f"{issue_id}: ") and "missing" in expected

    result = runner.invoke(app, ["validate", str(repo), issue_id])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [f"UNREADY {expected}"]
    assert "done (0 ready, 0 exempt, 1 unready, 0 error)" in result.stderr


def test_validate_no_id_sweeps_queue_and_epic_exempt(tmp_path: Path) -> None:
    """AC-3: with no id every open issue is judged; the epic reports EXEMPT,
    its ready children READY, and one unready leaf fails the sweep."""
    workspace = copy_bd_workspace(tmp_path / "repo", "epic")
    repo = workspace.path
    epic, ready, blocked = workspace.issues
    unready = _create_unready_issue(repo, "hand authored leaf")

    result = runner.invoke(app, ["validate", str(repo)])

    assert result.exit_code == 1, result.stdout + result.stderr
    rows = result.stdout.splitlines()
    assert f"EXEMPT {epic} (epic)" in rows
    assert f"READY {ready}" in rows
    assert f"READY {blocked}" in rows
    assert any(row.startswith(f"UNREADY {unready}: ") for row in rows), rows
    assert len(rows) == 4
    assert "done (2 ready, 1 exempt, 1 unready, 0 error)" in result.stderr


def test_validate_no_id_all_ready_exits_zero(tmp_path: Path) -> None:
    """A queue of ready leaves under an epic gates a run: exit 0."""
    workspace = copy_bd_workspace(tmp_path / "repo", "epic")

    result = runner.invoke(app, ["validate", str(workspace.path)])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert _rows(result.stdout).keys() == {"EXEMPT", "READY"}


def test_validate_missing_id_errors_and_continues(tmp_path: Path) -> None:
    """A nonexistent id is one named error row; the rest are still judged."""
    workspace = copy_bd_workspace(tmp_path / "repo", "leaf")
    repo, issue_id = workspace.path, workspace.issues[0]

    result = runner.invoke(app, ["validate", str(repo), "fixture-nope", issue_id])

    assert result.exit_code == 1, result.stdout + result.stderr
    rows = result.stdout.splitlines()
    assert len(rows) == 2
    assert rows[0].startswith("ERROR fixture-nope: ") and "fixture-nope" in rows[0]
    assert "Traceback" not in result.stdout + result.stderr
    assert rows[1] == f"READY {issue_id}"


def test_validate_json_is_pipe_clean(tmp_path: Path) -> None:
    """`--json` puts one parseable object on stdout, error rows included."""
    workspace = copy_bd_workspace(tmp_path / "repo", "leaf")
    repo, issue_id = workspace.path, workspace.issues[0]

    result = runner.invoke(
        app, ["validate", str(repo), issue_id, "fixture-nope", "--json"]
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    by_id = {row["id"]: row for row in payload["issues"]}
    assert by_id[issue_id] == {
        "id": issue_id,
        "status": "ready",
        "ok": True,
        "detail": issue_id,
    }
    assert by_id["fixture-nope"]["status"] == "error"
    assert by_id["fixture-nope"]["ok"] is False
    assert "fixture-nope" in by_id["fixture-nope"]["detail"]


def test_validate_empty_queue_is_not_an_error(tmp_path: Path) -> None:
    """No open issue and no id: a 'nothing to validate' line and exit 0."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path

    result = runner.invoke(app, ["validate", str(repo)])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.startswith("nothing to validate")
    assert "done (nothing to validate)" in result.stderr

    as_json = runner.invoke(app, ["validate", str(repo), "--json"])
    assert as_json.exit_code == 0, as_json.stdout + as_json.stderr
    assert json.loads(as_json.stdout) == {"ok": True, "issues": []}


def test_validate_requires_a_bd_workspace(tmp_path: Path) -> None:
    """Unlike `ortus spec`, the verb reads bd, so no .beads/ is exit 1."""
    result = runner.invoke(app, ["validate", str(tmp_path)])

    assert result.exit_code == 1
    assert "no .beads/ workspace at" in result.stderr


def test_validate_malformed_issue_reports_unready_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validator exception becomes an UNREADY row carrying its text."""
    workspace = copy_bd_workspace(tmp_path / "repo", "leaf")
    repo, issue_id = workspace.path, workspace.issues[0]

    def _explode(issue: dict) -> None:
        raise ValueError("acceptance_criteria is not text")

    monkeypatch.setattr(validate_mod, "validate_issue", _explode)
    result = runner.invoke(app, ["validate", str(repo), issue_id])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        f"UNREADY {issue_id}: acceptance_criteria is not text"
    ]
    assert "Traceback" not in result.stdout + result.stderr


def test_validate_is_discoverable() -> None:
    """The verb is in `ortus --help` and the README verb table."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "validate" in result.stdout
    text = README.read_text(encoding="utf-8")
    verbs = text[text.index("## The verbs") : text.index("## Prerequisites")]
    assert "ortus validate" in verbs
