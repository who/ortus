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

    def _run(
        self, *args: str, hooks: bool = True
    ) -> subprocess.CompletedProcess[str]:
        # `errors="replace"`: git echoes paths and hook output verbatim, which
        # is not guaranteed to be valid UTF-8. Strict decoding would raise
        # inside a helper whose contract is to answer conservatively, and would
        # do it precisely when we are trying to report someone else's failure.
        #
        # `hooks=False` disables repository hooks for the transaction's own
        # branch plumbing (branch creation, checkout, fast-forward). Beads
        # ≥1.0.4 installs a post-checkout hook that re-imports the exported
        # JSONL on branch switches; fired from the claim's checkout it
        # re-imported a stale export over the just-written claim — reverting
        # the issue to open and silently skipping verification. Commit-time
        # and push-time hooks stay live: a repository's refusing pre-commit
        # hook is an answer finalization must carry to the operator
        # (ortus-pgqg), and both coexisted with the pipeline all along. What
        # may not fire is a hook that rewrites tracker state on ref moves the
        # transaction itself performs.
        command = [self.binary, *args]
        if not hooks:
            command = [self.binary, "-c", "core.hooksPath=/dev/null", *args]
        return subprocess.run(
            command,
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

    def remote_tip(self, branch: str) -> str:
        """origin/<branch>'s last-known tip object id, or "" when unresolvable.

        Read from the local remote-tracking ref, never the network, so a push
        announcement can name the range about to leave (old remote tip .. local
        tip) without a round trip. "" covers a branch's first push (no tracking
        ref yet) and repos with no remote; callers say "all history" rather
        than inventing a range.
        """
        proc = self._run(
            "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def merge_base(self, ref_a: str, ref_b: str) -> str:
        """The merge-base commit of the two refs, or "" when none resolves.

        Empty on any error (unknown ref, unrelated histories, not a git
        repo), matching this client's conservative-answer convention; the
        caller decides whether a missing base is fatal.
        """
        proc = self._run("merge-base", ref_a, ref_b)
        return proc.stdout.strip() if proc.returncode == 0 else ""

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
        """`git checkout <branch>`, hook-free. Returns True on success."""
        return self._run("checkout", branch, hooks=False).returncode == 0

    def checkout_reporting(self, branch: str) -> str:
        """`git checkout <branch>`, hook-free; "" on success, else git's reason.

        The bare-bool `checkout` left an operator reproducing the failure by
        hand to learn why (the ortus-pgqg lesson, applied late): a claim that
        cannot switch branches must say what git said.
        """
        proc = self._run("checkout", branch, hooks=False)
        if proc.returncode == 0:
            return ""
        return _bounded_failure(proc) or f"git checkout exited {proc.returncode}"

    def clone_shared(self, ref: str, target: Path) -> str:
        """Materialize `ref` as a disposable shared clone at `target`.

        ``git clone --shared`` keeps the object store in the source repository
        (an ``objects/info/alternates`` pointer), so the clone is an ordinary
        directory that plain removal deletes — never a ``git worktree``, whose
        cleanup is unrecoverable under the sandbox (ortus-z7ib), and never a
        ``git archive``, whose tree strips the vcs metadata a hatch-vcs build
        derives its version from, so nothing in it could run. ``--no-checkout``
        plus a detached checkout keeps the clone's HEAD off any branch name the
        source repository owns.

        Returns "" on success, else the reason. An existing `target` is
        refused rather than overwritten.
        """
        if target.exists():
            return f"clone target already exists: {target}"
        # Resolve in the source repository before cloning: the clone only sees
        # `ref` as `origin/<ref>`, and a bare `checkout --detach <ref>` there
        # trips git's create-a-local-branch guessing instead of detaching.
        resolved = self._run("rev-parse", "--verify", f"{ref}^{{commit}}")
        if resolved.returncode != 0:
            return (
                _bounded_failure(resolved)
                or f"cannot resolve {ref!r} to a commit"
            )
        oid = resolved.stdout.strip()
        cloned = self._run(
            "clone", "--shared", "--no-checkout", str(self.repo), str(target),
            hooks=False,
        )
        if cloned.returncode != 0:
            return _bounded_failure(cloned) or f"git clone exited {cloned.returncode}"
        checked_out = self._run(
            "-C", str(target), "checkout", "--detach", oid, hooks=False
        )
        if checked_out.returncode != 0:
            return (
                _bounded_failure(checked_out)
                or f"git checkout exited {checked_out.returncode}"
            )
        return ""

    def delete_merged_branch(self, name: str) -> bool:
        """`git branch -d <name>` — refuses unmerged work by construction."""
        return self._run("branch", "-d", name).returncode == 0

    def switch_preserving_exports(
        self, branch: str, exports: frozenset[str]
    ) -> str:
        """Switch branches carrying the worktree's tracker exports along.

        Tracker exports are generated files: regenerable from the database,
        excluded from every candidate, and continuously rewritten by intake.
        When the current and target branches disagree about their committed
        content, a plain checkout refuses rather than overwrite the local
        copies — the race that halted a claim on 2026-08-11. The worktree's
        bytes are the newest export, so they win: snapshot them, clear the
        local modifications, switch hook-free, and write the snapshot back.
        Only export paths receive this treatment; any other local change
        still refuses the switch exactly as before.

        Returns "" on success, else the reason the switch failed.
        """

        snapshot: dict[str, bytes | None] = {}
        for rel in sorted(exports):
            path = self.repo / rel
            try:
                snapshot[rel] = path.read_bytes()
            except FileNotFoundError:
                snapshot[rel] = None
            except OSError as exc:
                return f"could not snapshot {rel} before switching ({exc})"
        # Make index and worktree match the current HEAD for each export, so
        # the switch sees no local change at all — an unstaged deletion blocks
        # a checkout exactly like an edit does. Paths HEAD does not track are
        # unstaged and removed instead; either way the target branch
        # materializes its own version, which the snapshot then overwrites.
        for rel in sorted(exports):
            tracked = (
                self._run("cat-file", "-e", f"HEAD:{rel}").returncode == 0
            )
            if tracked:
                restored = self._run(
                    "restore", "--source=HEAD", "--staged", "--worktree", "--", rel
                )
                if restored.returncode != 0:
                    return (
                        f"could not reset {rel} before switching: "
                        + (_bounded_failure(restored) or "git restore failed")
                    )
            else:
                self._run("restore", "--staged", "--", rel)
                try:
                    (self.repo / rel).unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    return f"could not clear {rel} before switching ({exc})"
        reason = self.checkout_reporting(branch)
        if reason:
            # Put the snapshot back where it was; the caller still sees the
            # failure, but with its exports intact rather than half-cleared.
            for rel, data in snapshot.items():
                if data is not None:
                    (self.repo / rel).write_bytes(data)
            return reason
        for rel, data in snapshot.items():
            target = self.repo / rel
            if data is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        return ""

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

    def valid_branch_name(self, name: str) -> bool:
        """True when git accepts `name` as a branch name."""
        return self._run("check-ref-format", "--branch", name).returncode == 0

    def branch_exists(self, name: str) -> bool:
        """True when a local branch of that exact name exists."""
        proc = self._run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
        return proc.returncode == 0

    def branch_tip(self, name: str) -> str:
        """The branch's tip object id, or "" when the branch does not exist."""
        proc = self._run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def changed_paths(self, base: str) -> frozenset[str] | None:
        """Every path that differs between `base` and the working tree.

        Committed-and-then-modified files appear once; the caller unions this
        with :meth:`dirty_paths` to cover untracked files, which no
        base-relative diff can name. None on error, mirroring `dirty_paths`.
        """
        proc = self._run("diff", "--name-only", "-z", base, "--")
        if proc.returncode != 0:
            return None
        return frozenset(path for path in proc.stdout.split("\0") if path)

    def head_message(self) -> str:
        """The full commit message of HEAD, or "" when unreadable."""
        proc = self._run("log", "-1", "--format=%B")
        return proc.stdout if proc.returncode == 0 else ""

    def amend_message(self, message: str) -> bool:
        """Rewrite HEAD's commit message in place, touching nothing else."""
        return (
            self._run("commit", "--amend", "--no-edit", "-m", message).returncode == 0
        )

    def amend_paths(self, paths: frozenset[str]) -> bool:
        """Fold the named worktree paths into HEAD, keeping its message.

        Used for the transaction's own late files — the tracker exports the
        close step rewrites — so an issue still lands as one commit instead of
        stacking a housekeeping commit on top of the worker's.
        """
        if not paths:
            return True
        ordered = sorted(paths)
        if self._run("add", "--", *ordered).returncode != 0:
            return False
        return (
            self._run(
                "commit", "--amend", "--no-edit", "--only", "--", *ordered
            ).returncode
            == 0
        )

    def create_branch(self, name: str, at_ref: str) -> bool:
        """`git branch <name> <at_ref>`, never forced. Returns True on success.

        Refuses (returns False) when the branch already exists — reusing or
        reporting an existing branch is the caller's decision, and silently
        resetting one would invent history nobody made.
        """
        return self._run("branch", name, at_ref, hooks=False).returncode == 0

    def fast_forward(self, branch: str, to_ref: str) -> bool:
        """Fast-forward `branch` (not checked out) to `to_ref`. True on success.

        ``git fetch . <to_ref>:<branch>`` updates the ref without touching the
        working tree and refuses anything that is not a fast-forward — the only
        integration this client performs. Updating the ref first, then
        switching to it, means the switch is between two names for the same
        commit and can never conflict with concurrently-dirtied files (the
        tracker's asynchronous exports made a checkout-then-merge sequence
        racy). Refuses to update the currently checked-out branch.
        """
        return (
            self._run("fetch", ".", f"{to_ref}:{branch}", hooks=False).returncode
            == 0
        )

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
