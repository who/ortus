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
    assert any(
        "preserving dirty worktree for worker handoff" in item for item in messages
    )


def test_commit_paths_leaves_unrelated_dirty_paths_untouched(tmp_path: Path) -> None:
    """A path-scoped commit is the mechanism that lets grind hand a worker a
    dirty tree: work outside the owned set stays exactly as the operator left
    it — staged, unstaged, and untracked alike."""
    repo = _repo(tmp_path)
    (repo / "owned.py").write_text("OWNED = True\n")
    (repo / "staged-unrelated.txt").write_text("staged operator work\n")
    subprocess.run(["git", "add", "staged-unrelated.txt"], cwd=repo, check=True)
    (repo / "untracked-unrelated.txt").write_text("untracked operator work\n")
    git = GitClient(repo)

    assert git.commit_paths(frozenset({"owned.py"}), "repo-1: verified candidate")

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert committed == ["owned.py"]
    assert (repo / "staged-unrelated.txt").read_text() == "staged operator work\n"
    assert (repo / "untracked-unrelated.txt").read_text() == "untracked operator work\n"
    dirty = git.dirty_paths()
    assert dirty == frozenset({"staged-unrelated.txt", "untracked-unrelated.txt"})
    assert git.status_text().splitlines() == [
        "A  staged-unrelated.txt",
        "?? untracked-unrelated.txt",
    ]


def _refusing_hook(repo: Path, body: str) -> None:
    """Install a pre-commit hook that refuses with `body` on stderr.

    A refusing hook is the failure that stranded a downstream finalization
    (ortus-pgqg): git's exit status alone says nothing about why.
    """
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\n{body}\nexit 1\n")
    hook.chmod(0o755)


def test_commit_paths_names_the_failing_command(tmp_path: Path) -> None:
    """AC-1: `add`, `diff --cached` and `commit` fail for different reasons, so
    a refusal has to say which one refused."""
    repo = _repo(tmp_path)
    (repo / "owned.py").write_text("OWNED = True\n")
    _refusing_hook(repo, 'echo "hook refused: lint failed" >&2')

    result = GitClient(repo).commit_paths(frozenset({"owned.py"}), "repo-1: candidate")

    assert not result, "a refused commit must be falsy at every boolean call site"
    assert result.command == "commit"
    assert result.reason.startswith("git commit exited 1")
    assert _subjects(repo)[0] == "baseline", "nothing may be committed"


def test_commit_paths_carries_git_stderr(tmp_path: Path) -> None:
    """AC-2: the operator gets git's own text, not a translation of it."""
    repo = _repo(tmp_path)
    (repo / "owned.py").write_text("OWNED = True\n")
    _refusing_hook(repo, 'echo "refusing: src/odd [name].py is unformatted" >&2')

    result = GitClient(repo).commit_paths(frozenset({"owned.py"}), "repo-1: candidate")

    assert "refusing: src/odd [name].py is unformatted" in result.reason


def test_commit_paths_bounds_captured_output(tmp_path: Path) -> None:
    """AC-3: a hook that prints a full lint report must not flood the log, and
    the reason git and hooks put first must survive the bound."""
    repo = _repo(tmp_path)
    (repo / "owned.py").write_text("OWNED = True\n")
    _refusing_hook(
        repo,
        'echo "refused: 400 findings" >&2\n'
        'for i in $(seq 1 400); do echo "finding $i: a long explanatory line" >&2; done',
    )

    result = GitClient(repo).commit_paths(frozenset({"owned.py"}), "repo-1: candidate")

    assert "refused: 400 findings" in result.detail
    assert result.detail.endswith("[truncated]")
    assert len(result.detail) < 600
    assert "finding 300" not in result.detail


def test_commit_paths_empty_stderr_still_names_command(tmp_path: Path) -> None:
    """AC-6: a git that fails silently still tells the operator which step of
    the path-scoped commit it was."""
    repo = _repo(tmp_path)
    (repo / "owned.py").write_text("OWNED = True\n")

    result = GitClient(repo, binary="false").commit_paths(
        frozenset({"owned.py"}), "repo-1: candidate"
    )

    assert not result
    assert result.command == "add"
    assert result.detail == ""
    assert result.reason == "git add exited 1 without a message"


def test_commit_paths_survives_non_utf8_git_output(tmp_path: Path) -> None:
    """Hook output is arbitrary bytes; reporting it may not raise."""
    repo = _repo(tmp_path)
    (repo / "owned.py").write_text("OWNED = True\n")
    _refusing_hook(repo, "printf 'refused \\377\\376 bytes\\n' >&2")

    result = GitClient(repo).commit_paths(frozenset({"owned.py"}), "repo-1: candidate")

    assert not result
    assert result.reason.startswith("git commit exited 1: refused")


def test_successful_commit_paths_reports_no_failure(tmp_path: Path) -> None:
    """AC-7's helper half: success carries nothing for a caller to log."""
    repo = _repo(tmp_path)
    (repo / "owned.py").write_text("OWNED = True\n")
    git = GitClient(repo)

    result = git.commit_paths(frozenset({"owned.py"}), "repo-1: candidate")

    assert result and result.reason == "" and result.command == ""
    # An empty candidate never touches git, and is not a failure either.
    assert git.commit_paths(frozenset(), "repo-1: nothing").reason == ""


def test_status_text_is_bounded_and_empty_outside_a_repo(tmp_path: Path) -> None:
    """The handoff prompt shares Claude's 4,000-character /goal budget, so the
    status it carries is truncated rather than unbounded."""
    repo = _repo(tmp_path)
    for index in range(40):
        (repo / f"file-{index:02d}.txt").write_text("dirty\n")

    text = GitClient(repo).status_text(limit=120)

    assert len(text) <= 120 + len("\n[truncated]")
    assert text.endswith("[truncated]")
    assert GitClient(tmp_path).status_text() == "", "outside a repo there is no status"


def test_failed_git_status_is_not_treated_as_clean(tmp_path: Path) -> None:
    git = GitClient(tmp_path, binary="false")

    assert git.dirty_paths() is None
    assert not git.is_clean()


def _with_remote(repo: Path, tmp_path: Path) -> Path:
    """Give `repo` an origin that already carries its baseline commit."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", remote], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True)
    return remote


def _move_origin_ahead(remote: Path, tmp_path: Path) -> None:
    """Land a commit on origin from a different checkout."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "ortus-tests@example.invalid"],
        cwd=other,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Ortus Tests"], cwd=other, check=True)
    (other / "elsewhere.py").write_text("REMOTE = True\n")
    subprocess.run(["git", "add", "-A"], cwd=other, check=True)
    subprocess.run(
        ["git", "commit", "-m", "remote moved"], cwd=other, check=True, capture_output=True
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=other, check=True)


def test_pull_rebase_replays_a_rejected_push_onto_the_moved_remote(
    tmp_path: Path,
) -> None:
    """The finalization sync path: origin moved while the transaction held the
    flock, so the push is rejected, the rebase replays the local commit, and the
    retried push lands both."""
    repo = _repo(tmp_path)
    remote = _with_remote(repo, tmp_path)
    _move_origin_ahead(remote, tmp_path)
    (repo / "candidate.py").write_text("CANDIDATE = True\n")
    subprocess.run(["git", "add", "candidate.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "verified candidate"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    git = GitClient(repo)

    assert not git.push("main"), "a non-fast-forward push must be reported as failed"
    assert git.pull_rebase("main")
    assert git.push("main")

    subjects = _subjects(repo)
    assert subjects[0] == "verified candidate"
    assert "remote moved" in subjects, "the remote commit survives the rebase"
    assert git.local_ahead_of_remote("main") == 0


def test_pull_rebase_refuses_to_rebase_over_a_dirty_worktree(tmp_path: Path) -> None:
    """Conservative branch: unrelated operator edits must never be rebased over.

    Returning False here is what leaves grind holding a recoverable journal
    instead of rewriting someone else's uncommitted work.
    """
    repo = _repo(tmp_path)
    remote = _with_remote(repo, tmp_path)
    _move_origin_ahead(remote, tmp_path)
    (repo / "source.py").write_text("OPERATOR_EDIT = True\n")
    git = GitClient(repo)

    assert not git.pull_rebase("main")
    assert (repo / "source.py").read_text() == "OPERATOR_EDIT = True\n"


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


# ---------------------------------------------------------------------------
# Branch helpers for branch-scoped finalization (ortus-rcdf.1)
# ---------------------------------------------------------------------------


def test_create_branch_never_forces(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    git = GitClient(repo)
    assert git.create_branch("ortus/tmpl-x", "main")
    assert git.branch_exists("ortus/tmpl-x")
    assert git.branch_tip("ortus/tmpl-x") == git.head_oid()
    # A second create of the same name refuses instead of resetting.
    (repo / "source.py").write_text("BASELINE = False\n")
    subprocess.run(["git", "commit", "-am", "move main"], cwd=repo, check=True)
    assert not git.create_branch("ortus/tmpl-x", "main")
    assert git.branch_tip("ortus/tmpl-x") != git.head_oid()


def test_branch_helpers_answer_conservatively(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    git = GitClient(repo)
    assert not git.branch_exists("ortus/absent")
    assert git.branch_tip("ortus/absent") == ""
    assert git.valid_branch_name("ortus/tmpl-a.1")
    assert not git.valid_branch_name("ortus/bad..name")


def test_fast_forward_updates_the_ref_without_touching_the_tree(
    tmp_path: Path,
) -> None:
    """`fast_forward` moves the target ref while another branch is checked
    out, leaves dirty files alone, and refuses a non-fast-forward."""
    repo = _repo(tmp_path)
    git = GitClient(repo)
    assert git.create_branch("ortus/tmpl-y", "main")
    subprocess.run(["git", "checkout", "ortus/tmpl-y"], cwd=repo, check=True)
    (repo / "source.py").write_text("CANDIDATE = True\n")
    subprocess.run(["git", "commit", "-am", "candidate"], cwd=repo, check=True)
    # A file dirtied while on the branch survives the ref update untouched.
    tracker = repo / ".beads" / "issues.jsonl"
    tracker.write_text("baseline\nasync export\n")
    assert git.fast_forward("main", "ortus/tmpl-y")
    assert git.branch_tip("main") == git.head_oid()
    assert tracker.read_text() == "baseline\nasync export\n"
    # The checkout that follows switches between two names for one commit.
    assert git.checkout("main")
    assert git.current_branch() == "main"
    # Diverged history is refused, never merged.
    subprocess.run(["git", "checkout", "-b", "diverged", "main~1"], cwd=repo, check=True)
    (repo / "other.py").write_text("OTHER = True\n")
    subprocess.run(["git", "add", "other.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "diverge"], cwd=repo, check=True)
    assert not git.fast_forward("main", "diverged")


def test_exports_survive_switch_byte_identically(tmp_path: Path) -> None:
    """ortus-mfyu AC-2: the worktree's newest export bytes cross the switch
    even when both branches' committed copies diverge from them."""
    repo = _repo(tmp_path)
    git = GitClient(repo)
    exports = repo / ".beads" / "issues.jsonl"
    subprocess.run(["git", "checkout", "-q", "-b", "ortus/tmpl-s"], cwd=repo, check=True)
    exports.write_text("baseline\nbranch-commit\n")
    subprocess.run(["git", "commit", "-aqm", "branch exports"], cwd=repo, check=True)
    newest = b"baseline\nbranch-commit\nnewest-staged\n"
    exports.write_bytes(newest)
    subprocess.run(["git", "add", ".beads/issues.jsonl"], cwd=repo, check=True)

    reason = git.switch_preserving_exports(
        "main", frozenset({".beads/issues.jsonl"})
    )

    assert reason == ""
    assert git.current_branch() == "main"
    assert exports.read_bytes() == newest
