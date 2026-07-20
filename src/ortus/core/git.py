"""Thin wrapper over the `git` CLI for grind's branch discipline.

grind workers commit + push the work that closes their issue. A worker that
drifts onto a feature branch (e.g. ``git checkout -b feature``) commits there
and pushes that branch, leaving origin/main — where deploys come from — stale.
Every "closed" issue then sits off the deploy path and the operator keeps
seeing supposedly-fixed bugs.

The outer loop uses this client to read the working tree's branch state, pin
it back to the integration branch each iteration, and push the integration
branch so a close is always deployable. This module is IO only; the branch
state is classified by :func:`ortus.core.grind_loop.classify_branch_state`
(pure logic, unit-test surface).

Every method is tolerant: if `git` is missing, the directory is not a git
repo, or a ref can't be resolved, we return a conservative value (False / "" /
0) rather than raise. grind operates on repos that may not be git-backed at
all (bd-only fixtures), and branch discipline must simply no-op there.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ortus.core.grind_loop import BranchState, DEFAULT_INTEGRATION_BRANCH

_RUNTIME_PATHS = (
    "logs",
    ".cache",
    ".beads/ortus.flock",
)

_WORKER_PATHSPECS = (
    ".",
    *tuple(f":(exclude){path}" for path in _RUNTIME_PATHS),
)


@dataclass
class GitClient:
    """Thin typed surface over the git CLI, scoped to a single repo dir."""

    repo: Path
    binary: str = "git"

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=False,
        )

    # --- reads ----------------------------------------------------------

    def is_git_repo(self) -> bool:
        """True when `repo` is inside a git work tree.

        When False the whole branch-discipline path is skipped — grind is
        sometimes pointed at bd-only fixtures that were never `git init`'d.
        """
        proc = self._run("rev-parse", "--is-inside-work-tree")
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def has_commits(self) -> bool:
        """True when HEAD resolves to a commit.

        False on an *unborn* branch — a freshly ``git init``'d repo that has no
        commits yet (e.g. immediately after ``ortus init``). Such a repo can't
        have stranded any work, so branch discipline must no-op rather than
        trip: on an unborn branch ``git rev-parse --abbrev-ref HEAD`` fails and
        :meth:`current_branch` returns "", which would otherwise be
        misclassified as a detached HEAD and HALT the loop.
        """
        return self._run("rev-parse", "--verify", "--quiet", "HEAD").returncode == 0

    def has_remote(self) -> bool:
        """True when at least one git remote is configured."""
        proc = self._run("remote")
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def is_clean(self) -> bool:
        """True when no non-runtime worktree changes exist."""
        return self.dirty_paths() == frozenset()

    def dirty_paths(self) -> frozenset[str] | None:
        """Return every staged, unstaged, or untracked non-runtime path.

        Porcelain ``-z`` output avoids quoting and whitespace ambiguity. Rename
        and copy entries contain a second path record; both paths are retained
        so ownership checks fail safely unless the complete operation is
        allowlisted.
        """
        proc = self._run(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *_WORKER_PATHSPECS,
        )
        if proc.returncode != 0:
            return None
        records = proc.stdout.split("\0")
        paths: set[str] = set()
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                continue
            status = record[:2]
            paths.add(record[3:])
            if "R" in status or "C" in status:
                if index < len(records) and records[index]:
                    paths.add(records[index])
                index += 1
        return frozenset(paths)

    def current_branch(self) -> str:
        """Checked-out branch name, or "" for a detached HEAD / on error.

        `git rev-parse --abbrev-ref HEAD` prints the literal "HEAD" when
        detached; we normalize that to "" so the classifier's detached-HEAD
        branch fires.
        """
        proc = self._run("rev-parse", "--abbrev-ref", "HEAD")
        if proc.returncode != 0:
            return ""
        name = proc.stdout.strip()
        return "" if name == "HEAD" else name

    def _count(self, *rev_args: str) -> int:
        """`git rev-list --count <rev_args>` → int, 0 on any error.

        Used for both stray-commit and ahead-of-remote counts; an unresolvable
        ref (e.g. integration branch absent, or origin not fetched) yields 0,
        the conservative "nothing stranded / nothing to push" answer.
        """
        proc = self._run("rev-list", "--count", *rev_args)
        if proc.returncode != 0:
            return 0
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return 0

    def stray_commit_count(self, integration_branch: str) -> int:
        """Commits reachable from HEAD but not from the integration branch.

        Non-zero only when the current branch has carried work past the
        integration branch — i.e. a worker committed somewhere other than the
        integration branch. 0 when on the integration branch or when the side
        branch's commits are already merged in.
        """
        return self._count(f"{integration_branch}..HEAD")

    def local_ahead_of_remote(self, branch: str) -> int:
        """Commits `branch` is ahead of origin/<branch>.

        Non-zero means the integration branch has local commits not yet on
        origin (a worker committed but didn't push, or pushed elsewhere). 0
        when in sync, or when origin/<branch> can't be resolved (no remote,
        not fetched) — branch discipline never blocks on an unknown remote.
        """
        return self._count(f"origin/{branch}..{branch}")

    def branch_state(
        self, integration_branch: str = DEFAULT_INTEGRATION_BRANCH
    ) -> BranchState:
        """Gather the three signals the branch-discipline classifier needs."""
        current = self.current_branch()
        return BranchState(
            current_branch=current,
            stray_commits=self.stray_commit_count(integration_branch),
            local_ahead_of_remote=self.local_ahead_of_remote(integration_branch),
            integration_branch=integration_branch,
        )

    # --- writes ---------------------------------------------------------

    def checkout(self, branch: str) -> bool:
        """`git checkout <branch>`. Returns True on success."""
        return self._run("checkout", branch).returncode == 0

    def push(self, branch: str) -> bool:
        """`git push origin <branch>`. Returns True on success.

        A failed push (e.g. non-fast-forward because origin moved) is surfaced
        by the caller as a loud warning rather than silently swallowed — an
        unpushed close is exactly the stranded-work condition this feature
        exists to make visible.
        """
        return self._run("push", "origin", branch).returncode == 0

    def commit_all(self, message: str) -> bool:
        """Stage and commit the current iteration's changes.

        The Codex grind path calls this only after verifying that the tree was
        clean before the worker ran, so ``git add -A`` cannot absorb unrelated
        operator work.
        """
        if self._run("add", "-A").returncode != 0:
            return False
        if self._run("reset", "--quiet", "--", *_RUNTIME_PATHS).returncode != 0:
            return False
        # A close can occasionally be persisted outside the git worktree. In
        # that case there is nothing to commit and the iteration is still safe.
        if self._run("diff", "--cached", "--quiet").returncode == 0:
            return True
        return self._run("commit", "-m", message).returncode == 0

    def commit_paths(self, paths: frozenset[str], message: str) -> bool:
        """Commit only explicitly owned paths, preserving everything else."""
        if not paths:
            return True
        ordered = sorted(paths)
        if self._run("add", "--", *ordered).returncode != 0:
            return False
        staged = self._run("diff", "--cached", "--name-only", "-z")
        if staged.returncode != 0:
            return False
        staged_paths = frozenset(path for path in staged.stdout.split("\0") if path)
        if not staged_paths.issubset(paths):
            return False
        if not staged_paths:
            return True
        return self._run("commit", "-m", message).returncode == 0
