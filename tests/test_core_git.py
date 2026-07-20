"""Git ownership boundaries used by Codex Grind preflight."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from ortus.commands.grind import (
    _checkpoint_codex_preflight,
    _enforce_branch_discipline,
)
from ortus.core.git import GitClient
from ortus.core.grind_loop import BranchState


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ortus-tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Ortus Tests"], cwd=repo, check=True)
    (repo / ".beads").mkdir()
    (repo / ".beads" / "issues.jsonl").write_text("baseline\n")
    (repo / "source.py").write_text("BASELINE = True\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _subjects(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def test_tracker_only_preflight_creates_one_idempotent_housekeeping_commit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    tracker = repo / ".beads" / "issues.jsonl"
    tracker.write_text("baseline\nexported update\n")
    subprocess.run(["git", "add", str(tracker)], cwd=repo, check=True)
    git = GitClient(repo)
    messages: list[str] = []

    _checkpoint_codex_preflight(git, "main", messages.append)

    assert git.is_clean()
    assert _subjects(repo)[0] == "chore: sync beads state"
    assert any("housekeeping commit completed" in message for message in messages)
    count = len(_subjects(repo))

    _checkpoint_codex_preflight(git, "main", messages.append)

    assert len(_subjects(repo)) == count


def test_mixed_tracker_and_source_changes_halt_without_committing(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    tracker = repo / ".beads" / "issues.jsonl"
    tracker.write_text("baseline\nexported update\n")
    subprocess.run(["git", "add", str(tracker)], cwd=repo, check=True)
    (repo / "source.py").write_text("BASELINE = False\n")
    git = GitClient(repo)
    before = _subjects(repo)

    with pytest.raises(typer.Exit):
        _checkpoint_codex_preflight(git, "main", lambda _message: None)

    assert _subjects(repo) == before
    assert git.dirty_paths() == frozenset({".beads/issues.jsonl", "source.py"})


def test_preflight_reports_tracker_commit_failure() -> None:
    class FailingGit:
        def is_git_repo(self) -> bool:
            return True

        def dirty_paths(self) -> frozenset[str]:
            return frozenset({".beads/issues.jsonl"})

        def commit_paths(self, paths: frozenset[str], message: str) -> bool:
            assert paths == frozenset({".beads/issues.jsonl"})
            assert message == "chore: sync beads state"
            return False

    messages: list[str] = []
    with pytest.raises(typer.Exit):
        _checkpoint_codex_preflight(  # type: ignore[arg-type]
            FailingGit(), "main", messages.append
        )

    assert any("housekeeping commit failed" in message for message in messages)


def test_commit_paths_preserves_an_unowned_pre_staged_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tracker = repo / ".beads" / "issues.jsonl"
    tracker.write_text("baseline\nexported update\n")
    (repo / "source.py").write_text("BASELINE = False\n")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    git = GitClient(repo)

    assert git.commit_paths(
        frozenset({".beads/issues.jsonl"}), "chore: sync beads state"
    )
    assert _subjects(repo)[0] == "chore: sync beads state"
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", "source.py"], cwd=repo
        ).returncode
        == 1
    )


def test_dirty_source_becomes_preserved_codex_baseline(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tracker = repo / ".beads" / "issues.jsonl"
    tracker.write_text("baseline\nexported update\n")
    (repo / "source.py").write_text("BASELINE = False\n")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    git = GitClient(repo)
    messages: list[str] = []

    baseline = _checkpoint_codex_preflight(
        git, "main", messages.append, accept_baseline=True
    )

    assert baseline == frozenset({"source.py"})
    assert _subjects(repo)[0] == "chore: sync beads state"
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", "source.py"], cwd=repo
        ).returncode
        == 1
    )
    assert any("preserving dirty operator baseline" in item for item in messages)


def test_failed_git_status_is_not_treated_as_clean(tmp_path: Path) -> None:
    git = GitClient(tmp_path, binary="false")

    assert git.dirty_paths() is None
    assert not git.is_clean()


def test_failed_housekeeping_push_halts_before_work() -> None:
    class PushFailingGit:
        def is_git_repo(self) -> bool:
            return True

        def has_commits(self) -> bool:
            return True

        def branch_state(self, integration_branch: str) -> BranchState:
            return BranchState(
                current_branch=integration_branch,
                stray_commits=0,
                local_ahead_of_remote=1,
                integration_branch=integration_branch,
            )

        def has_remote(self) -> bool:
            return True

        def push(self, branch: str) -> bool:
            assert branch == "main"
            return False

    with pytest.raises(typer.Exit):
        _enforce_branch_discipline(  # type: ignore[arg-type]
            PushFailingGit(), "main", lambda _message: None, phase="test"
        )
