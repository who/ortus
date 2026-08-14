"""Worker-owned close on main; grind does not Ortus-finalize (ortus-88ml).

f2he retired harness claim, issue-branch workspaces, and Ortus-owned
report/close/commit/sync. The worker session-closes. These tests drive that
contract through `ortus grind` and keep I/O out of the assertions that prove
the land.
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
from ortus.core.sandbox import SandboxInfo
from tests.conftest import copy_bd_workspace

pytestmark = [pytest.mark.integration, pytest.mark.slow]
runner = CliRunner()

CANDIDATE = "candidate.py"
WORKER_SUBJECT = "worker ships the candidate on main"


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


def _subjects(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _current_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _issue_branches(repo: Path, issue_id: str) -> str:
    return subprocess.run(
        ["git", "branch", "--list", f"ortus/{issue_id}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _grind_log(repo: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )


def _select_issue(host: Path) -> str:
    listing = json.loads(
        subprocess.run(
            ["bd", "list", "--status=in_progress", "--json"],
            cwd=host,
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
    if issue_id:
        return issue_id
    ready = json.loads(
        subprocess.run(
            ["bd", "ready", "--json"],
            cwd=host,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        or "[]"
    )
    issue_id = next(
        str(item["id"])
        for item in ready
        if isinstance(item, dict) and item.get("issue_type") != "epic"
    )
    subprocess.run(
        ["bd", "update", issue_id, "--status", "in_progress"],
        cwd=host,
        check=True,
        capture_output=True,
    )
    return issue_id


class _CommitAndCloseWorker:
    extra_env: dict[str, str] = {}

    def __init__(self, host: Path) -> None:
        self.host = host
        self.branches: list[str] = []

    def run(self, prompt: str, **kwargs: object) -> int:
        del prompt, kwargs
        self.branches.append(_current_branch(self.host))
        issue_id = _select_issue(self.host)
        (self.host / CANDIDATE).write_text("SHIPPED = True\n", encoding="utf-8")
        subprocess.run(["git", "add", CANDIDATE], cwd=self.host, check=True)
        subprocess.run(
            ["git", "commit", "-m", WORKER_SUBJECT],
            cwd=self.host,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["bd", "close", issue_id, "--reason", "worker session-closed"],
            cwd=self.host,
            check=True,
            capture_output=True,
        )
        return 0


class _DirtyAndBailWorker:
    extra_env: dict[str, str] = {}

    def __init__(self, host: Path) -> None:
        self.host = host

    def run(self, prompt: str, **kwargs: object) -> int:
        del prompt, kwargs
        _select_issue(self.host)
        (self.host / CANDIDATE).write_text("UNFINISHED = True\n", encoding="utf-8")
        return 0


def test_worker_commit_and_close_on_main_is_the_only_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that commits on main and closes is the iteration win.

    Grind must not cut `ortus/<id>`, must not write a second finalization
    commit, and must count the worker's close.
    """
    repo, issue_id = _seed(tmp_path, "fin-land")
    before = _subjects(repo)
    worker = _CommitAndCloseWorker(repo)
    _install(monkeypatch, tmp_path, worker)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _bd_show(repo, issue_id)["status"] == "closed"
    assert f"worker closed {issue_id}" in _grind_log(repo)
    subjects = _subjects(repo)
    assert subjects[0] == WORKER_SUBJECT
    assert subjects[1:] == before
    assert not any(line.startswith(f"{issue_id}:") for line in subjects)
    assert _issue_branches(repo, issue_id) == ""
    assert _current_branch(repo) == "main"
    assert worker.branches == ["main"]
    assert (repo / CANDIDATE).read_text(encoding="utf-8") == "SHIPPED = True\n"


def test_grind_does_not_finalize_an_unclosed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfinished worker leaves the claim and the dirty tree. Grind does
    not close, commit, or invent an issue branch on the worker's behalf."""
    repo, issue_id = _seed(tmp_path, "fin-unclosed")
    before_head = _head(repo)
    _install(monkeypatch, tmp_path, _DirtyAndBailWorker(repo))

    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _bd_show(repo, issue_id)["status"] == "in_progress"
    assert _head(repo) == before_head
    assert _issue_branches(repo, issue_id) == ""
    assert (repo / CANDIDATE).read_text(encoding="utf-8") == "UNFINISHED = True\n"
    log = _grind_log(repo)
    assert f"left {issue_id} in_progress for the next window" in log
    assert f"worker closed {issue_id}" not in log


def test_primary_checkout_stays_on_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared checkout never leaves the integration branch for a clone."""
    repo, issue_id = _seed(tmp_path, "fin-main")
    worker = _CommitAndCloseWorker(repo)
    _install(monkeypatch, tmp_path, worker)

    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert worker.branches == ["main"]
    assert _current_branch(repo) == "main"
    assert _issue_branches(repo, issue_id) == ""
    assert not (repo / "logs" / "grind-workspaces" / issue_id).exists()
