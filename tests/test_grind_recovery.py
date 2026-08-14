"""On-main leftover-claim resume after a failed grind window (ortus-88ml).

f2he made grind a respawn loop: the worker owns claim and close, work stays
on main, and a leftover in_progress is the next window's goal. These tests
drive that contract through `ortus grind` instead of the retired issue-branch,
grind-workspace, and Ortus-finalization machinery.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.bd import BdClient
from ortus.core.git import GitClient
from ortus.core.sandbox import SandboxInfo
from ortus.core.transaction import JournalStore
from tests._shims import ready_issue_args
from tests.conftest import copy_bd_workspace

pytestmark = [pytest.mark.integration, pytest.mark.slow]
runner = CliRunner()

LEFTOVER = "leftover.py"


def _fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _seed(tmp_path: Path, name: str) -> tuple[Path, str]:
    workspace = copy_bd_workspace(tmp_path / name, "leaf")
    repo = workspace.path
    (repo / ".gitignore").write_text(
        "logs/\n.cache/\n.beads/ortus.flock\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, workspace.issues[0]


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: object) -> None:
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: backend)


def _bd_show(repo: Path, issue_id: str) -> dict:
    data = json.loads(
        subprocess.run(
            ["bd", "show", issue_id, "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return data[0] if isinstance(data, list) else data


def _claim(repo: Path, issue_id: str) -> None:
    subprocess.run(
        ["bd", "update", issue_id, "--status", "in_progress"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _create_ready(repo: Path, title: str) -> str:
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
            *ready_issue_args(),
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _grind_log(repo: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )


def _write_schema1_journal(repo: Path, issue_id: str, *, paths: list[str]) -> None:
    payload = {
        "schema": 1,
        "issue_id": issue_id,
        "base_head": _head(repo),
        "baseline_paths": [],
        "baseline_fingerprints": {},
        "candidate_paths": paths,
        "phase": "implementation",
    }
    path = repo / "logs" / "grind-transaction.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class _RecordingContinueWorker:
    """Continue the leftover in_progress claim and record the prompt."""

    extra_env: dict[str, str] = {}

    def __init__(self, host: Path) -> None:
        self.host = host
        self.prompts: list[str] = []
        self.seen: list[str] = []

    def run(self, prompt: str, **kwargs: object) -> int:
        del kwargs
        self.prompts.append(prompt)
        listing = json.loads(
            subprocess.run(
                ["bd", "list", "--status=in_progress", "--json"],
                cwd=self.host,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            or "[]"
        )
        issue_id = next(
            (
                str(item["id"])
                for item in listing
                if isinstance(item, dict) and item.get("id")
            ),
            "",
        )
        self.seen.append(issue_id)
        return 0


def test_leftover_claim_with_dirty_work_is_resumed_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover in_progress plus uncommitted work is the next window's goal.

    Grind must spawn against that claim (not a newer ready leaf) and hand the
    worker a RECOVERY HANDOFF that names the inherited path.
    """
    repo, leftover_id = _seed(tmp_path, "rec-leftover")
    other_id = _create_ready(repo, "do not pick this one")
    _claim(repo, leftover_id)
    (repo / LEFTOVER).write_text("HALF_DONE = True\n", encoding="utf-8")
    worker = _RecordingContinueWorker(repo)
    _install(monkeypatch, tmp_path, worker)

    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert worker.prompts, "a leftover claim must spawn a worker"
    assert "RECOVERY HANDOFF" in worker.prompts[0]
    assert LEFTOVER in worker.prompts[0]
    assert worker.seen == [leftover_id]
    log = _grind_log(repo)
    assert f"continuing leftover claim {leftover_id}" in log
    assert _bd_show(repo, leftover_id)["status"] == "in_progress"
    assert _bd_show(repo, other_id)["status"] == "open"
    assert (repo / LEFTOVER).read_text(encoding="utf-8") == "HALF_DONE = True\n"


def test_historical_journal_schema_is_context_not_a_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-1 journal still names the leftover claim. The mismatch is a
    log note; grind must start and resume rather than refuse the tree."""
    repo, leftover_id = _seed(tmp_path, "rec-schema")
    _claim(repo, leftover_id)
    (repo / LEFTOVER).write_text("SCHEMA_DRIFT = True\n", encoding="utf-8")
    _write_schema1_journal(repo, leftover_id, paths=[LEFTOVER])
    worker = _RecordingContinueWorker(repo)
    _install(monkeypatch, tmp_path, worker)

    # Codex has no 4,000-character /goal cap. The schema-1 note is extra
    # handoff context and must not become a startup refusal; Claude's wrap
    # limit is a different surface.
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
    log = _grind_log(repo)
    assert "journal schema 1" in log
    assert "is not the supported" in log
    assert worker.prompts and "RECOVERY HANDOFF" in worker.prompts[0]
    assert f"continuing leftover claim {leftover_id}" in log
    assert _bd_show(repo, leftover_id)["status"] == "in_progress"


def test_off_main_tree_with_stray_commits_halts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work committed off the integration branch is stranded. Grind must HALT
    and leave bd untouched rather than silently re-checkout main."""
    repo, issue_id = _seed(tmp_path, "rec-offmain")
    subprocess.run(["git", "checkout", "-b", "stray"], cwd=repo, check=True)
    (repo / "stranded.txt").write_text("off the deploy path\n", encoding="utf-8")
    subprocess.run(["git", "add", "stranded.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "stranded work"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    worker = _RecordingContinueWorker(repo)
    _install(monkeypatch, tmp_path, worker)

    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 1, result.stdout + result.stderr
    assert worker.prompts == [], "a stranded checkout must not spawn a worker"
    log = _grind_log(repo)
    assert "HALT" in log
    assert "stray" in log
    assert _bd_show(repo, issue_id)["status"] == "open"
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current == "stray"


def test_stale_journal_does_not_reopen_a_closed_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal whose issue already closed is routing context, not a reopen."""
    repo, issue_id = _seed(tmp_path, "rec-closed")
    _claim(repo, issue_id)
    subprocess.run(
        ["bd", "close", issue_id, "--reason", "already shipped"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / LEFTOVER).write_text("STALE = True\n", encoding="utf-8")
    _write_schema1_journal(repo, issue_id, paths=[LEFTOVER])
    worker = _RecordingContinueWorker(repo)
    _install(monkeypatch, tmp_path, worker)

    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _bd_show(repo, issue_id)["status"] == "closed"
    log = _grind_log(repo)
    assert f"{issue_id} is already closed" in log
    assert worker.prompts == [], "a closed leftover must not be respawned"


def test_prepare_handoff_loads_a_schema1_journal_as_context(
    tmp_path: Path,
) -> None:
    """Helper: `_prepare_handoff` keeps the leftover identity when the journal
    schema is historical. This is context, not a refusal."""
    repo, leftover_id = _seed(tmp_path, "rec-handoff")
    _claim(repo, leftover_id)
    (repo / LEFTOVER).write_text("CONTEXT = True\n", encoding="utf-8")
    _write_schema1_journal(repo, leftover_id, paths=[LEFTOVER])
    notes: list[str] = []

    state = grind_mod._prepare_handoff(
        BdClient(repo=repo),
        GitClient(repo),
        JournalStore(repo),
        repo=repo,
        backend="claude",
        integration_branch="main",
        write_log=notes.append,
    )

    assert state.resume_issue_id == leftover_id
    assert LEFTOVER in state.handoff_paths
    assert any("journal schema 1" in line for line in notes)
    resumed = JournalStore(repo).load()
    assert resumed is not None
    assert resumed.issue_id == leftover_id
