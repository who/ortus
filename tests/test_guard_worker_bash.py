"""`ortus guard worker-bash`: a grind worker never rewrites pushed history."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.worker_guard import (
    ALLOW_FORCE_PUSH_ENV,
    GRIND_WORKER_ENV,
    bash_violation,
)

cli = CliRunner()

FORCED_PUSHES = (
    "git push --force",
    "git push -f origin main",
    "git push --force-with-lease",
    "git push --force-with-lease=main:abc123 origin main",
    "git push --force-if-includes origin main",
    "git push -fu origin main",
    "git -C /repo push -f",
    "git push origin +main",
    "git push --mirror",
    "git add . && git commit -m x && git push --force-with-lease",
    "GIT_TRACE=1 git push -f",
)

ALLOWED = (
    "git push",
    "git push origin main",
    "git push -u origin ortus/x",
    "git pushx",
    "echo git push --force",
    "ls -la",
    "bd close ortus-1 --reason done",
    "git status",
    "git commit -m 'fix'",
    "echo 'unterminated",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path, *, pushed: bool) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    if not pushed:
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-q", "-m", "second")
    return repo


@pytest.mark.parametrize("command", FORCED_PUSHES)
def test_force_push_spellings_are_refused(command: str, tmp_path: Path) -> None:
    violation = bash_violation(command, tmp_path)
    assert violation is not None
    assert "pushed commit is final" in violation


@pytest.mark.parametrize("command", ALLOWED)
def test_guard_allows_plain_pushes_and_other_commands(
    command: str, tmp_path: Path
) -> None:
    assert bash_violation(command, tmp_path) is None


def test_amend_of_pushed_head_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, pushed=True)
    violation = bash_violation("git commit --amend -m 'better'", repo)
    assert violation is not None and "remote-tracking" in violation


def test_amend_of_unpushed_head_is_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, pushed=False)
    assert bash_violation("git commit --amend --no-edit", repo) is None


def test_amend_without_any_remote_is_allowed(tmp_path: Path) -> None:
    repo = tmp_path / "solo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "only")
    assert bash_violation("git commit --amend --no-edit", repo) is None
    assert bash_violation("git push -f", repo) is not None


def _hook(command: str, env: dict[str, str], cwd: Path | None = None) -> int:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    result = cli.invoke(app, ["guard", "worker-bash"], input=json.dumps(payload), env=env)
    return result.exit_code


def test_guard_inactive_outside_a_grind_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(GRIND_WORKER_ENV, raising=False)
    monkeypatch.delenv(ALLOW_FORCE_PUSH_ENV, raising=False)
    assert _hook("git push --force", {}) == 0


def test_guard_inactive_with_the_operator_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {GRIND_WORKER_ENV: "1", ALLOW_FORCE_PUSH_ENV: "1"}
    assert _hook("git push --force", env) == 0


def test_hook_exit_code_is_2_on_violation_and_0_otherwise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ALLOW_FORCE_PUSH_ENV, raising=False)
    env = {GRIND_WORKER_ENV: "1"}
    assert _hook("git push --force-with-lease", env) == 2
    assert _hook("git push origin main", env) == 0
    repo = _repo(tmp_path, pushed=True)
    assert _hook("git commit --amend --no-edit", env, cwd=repo) == 2


@pytest.mark.parametrize("stdin", ["", "not json", json.dumps({"tool_name": "Edit"})])
def test_hook_exit_code_is_0_for_non_bash_payloads(
    stdin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_FORCE_PUSH_ENV, raising=False)
    result = cli.invoke(
        app, ["guard", "worker-bash"], input=stdin, env={GRIND_WORKER_ENV: "1"}
    )
    assert result.exit_code == 0


@pytest.mark.integration
def test_grind_env_marks_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_stealth_grind import _stealth_grind

    _, _, worker, result = _stealth_grind(
        tmp_path, monkeypatch, scope="Edit `src/a.py`.", touch=False
    )
    assert worker.envs, result.output
    assert worker.envs[0].get(GRIND_WORKER_ENV) == "1"
