"""Thin wrapper over the `git` CLI for grind's branch discipline.

Workers no longer commit or push: `ortus grind` itself commits the verified
candidate and synchronizes the integration branch. Branch discipline predates
that split and still guards the same failure — work that ends up somewhere
other than the integration branch (e.g. a worker or operator left the tree on
``feature``) leaves origin/main, where deploys come from, stale, so every
"closed" issue sits off the deploy path.

The outer loop uses this client to read the working tree's branch state, pin
it back to the integration branch each iteration, commit exactly the
transaction-owned paths, and push the integration branch so a close is always
deployable. This module is IO only; the branch state is classified by
:func:`ortus.core.grind_loop.classify_branch_state` (pure logic, unit-test
surface).

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

# A refusing pre-commit hook may print a whole lint report; the reason git or
# the hook gives is in the first lines, so keep those and drop the rest rather
# than flooding the run log with someone else's output.
_FAILURE_LINES = 5
_FAILURE_CHARS = 400


def _bounded_failure(proc: subprocess.CompletedProcess[str]) -> str:
    """git's own explanation for a failure, bounded to a loggable size.

    stderr is where git puts its errors, but a refusing hook usually prints to
    stdout, so fall back to it — the point of carrying this text at all is that
    the operator sees the actual reason.
    """
    text = proc.stderr.strip() or proc.stdout.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    joined = "; ".join(lines[:_FAILURE_LINES])
    truncated = len(lines) > _FAILURE_LINES
    if len(joined) > _FAILURE_CHARS:
        joined = joined[:_FAILURE_CHARS].rstrip()
        truncated = True
    return f"{joined} [truncated]" if truncated else joined


@dataclass(frozen=True)
class CommitResult:
    """Outcome of a path-scoped commit — falsy on failure, with git's reason.

    Falsy rather than raising because finalization treats a failed commit as a
    recoverable blocker, and every call site is written as ``if not
    git.commit_paths(...)``. :attr:`reason` names which git command refused and
    quotes what it said, so the operator does not have to reproduce the failure
    by hand to learn why (ortus-pgqg).
    """

    ok: bool
    command: str = ""
    returncode: int = 0
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def reason(self) -> str:
        """One line naming the failing command and git's message; "" on success."""
        if self.ok:
            return ""
        head = f"git {self.command} exited {self.returncode}"
        return f"{head}: {self.detail}" if self.detail else f"{head} without a message"


_COMMIT_OK = CommitResult(ok=True)


def _commit_failed(
    command: str, proc: subprocess.CompletedProcess[str]
) -> CommitResult:
    return CommitResult(
        ok=False,
        command=command,
        returncode=proc.returncode,
        detail=_bounded_failure(proc),
    )


@dataclass
class GitClient:
    """Thin typed surface over the git CLI, scoped to a single repo dir."""

    repo: Path
    binary: str = "git"

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # `errors="replace"`: git echoes paths and hook output verbatim, which
        # is not guaranteed to be valid UTF-8. Strict decoding would raise
        # inside a helper whose contract is to answer conservatively, and would
        # do it precisely when we are trying to report someone else's failure.
        return subprocess.run(
            [self.binary, *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            errors="replace",
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

    def status_text(self, *, limit: int = 1_200) -> str:
        """Bounded porcelain status for handoff context and operator logs.

        A fresh worker resuming someone else's uncommitted work needs to see the
        shape of the worktree — staged vs unstaged vs untracked — not just the
        path set `dirty_paths` returns. Bounded because it also goes into a
        prompt with a hard size budget.
        """
        proc = self._run(
            "status", "--porcelain=v1", "--untracked-files=all", "--", *_WORKER_PATHSPECS
        )
        if proc.returncode != 0:
            return ""
        text = proc.stdout.strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n[truncated]"

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

    def head_oid(self) -> str:
        """Return the current HEAD object id, or an empty string when unborn."""
        proc = self._run("rev-parse", "--verify", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else ""

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

    def pull_rebase(self, branch: str) -> bool:
        """`git pull --rebase origin <branch>`. Returns True on success.

        Only reached after a push was rejected: origin moved while the
        transaction held the flock. It fails (returning False) on a dirty
        worktree, which is the conservative answer — grind then halts with a
        recoverable journal rather than rebasing over an operator's own edits.
        """
        return self._run("pull", "--rebase", "origin", branch).returncode == 0

    def commit_paths(self, paths: frozenset[str], message: str) -> CommitResult:
        """Commit only explicitly owned paths, preserving everything else.

        ``git commit --only`` builds the commit from the named worktree paths
        while leaving unrelated staged changes in the index. That matters when
        grind started from an intentionally dirty operator checkout.

        Returns a :class:`CommitResult` that is falsy on failure and carries
        git's own explanation: ``add``, ``diff --cached`` and ``commit`` fail
        for different reasons, and the caller can only say something useful
        about a refused commit if it knows which one refused and why.
        """
        if not paths:
            return _COMMIT_OK
        ordered = sorted(paths)
        added = self._run("add", "--", *ordered)
        if added.returncode != 0:
            return _commit_failed("add", added)
        staged = self._run("diff", "--cached", "--name-only", "-z", "--", *ordered)
        if staged.returncode != 0:
            return _commit_failed("diff --cached", staged)
        staged_paths = frozenset(path for path in staged.stdout.split("\0") if path)
        if not staged_paths:
            return _COMMIT_OK
        committed = self._run("commit", "--only", "-m", message, "--", *ordered)
        if committed.returncode != 0:
            return _commit_failed("commit", committed)
        return _COMMIT_OK
