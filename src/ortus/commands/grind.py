"""ortus grind <repo> — subprocess-per-task outer loop (ortus-3ico pivot).

Each iteration spawns a fresh `claude -p "/goal CLOSE-ONE"` subprocess.
The outer Python loop trusts ONLY observable bd state (counts plus the
in_progress id set) to decide whether the iteration closed an issue,
orphaned a claim, or did nothing. Model claims, /goal evaluator judgments,
and transcript sentinels are never consulted.

This replaces the previous long-lived single-session shape (xvel.4 pre-pivot),
which carried a single claude session across the entire queue and was
vulnerable to context-rot past ~20-30 tasks. The pivot trades per-iteration
boot cost for a fresh context window per task and a structurally-detectable
orphan-claim failure mode.

Preserved invariants from the prior shape:
  - flock at .beads/ortus.flock (single-instance per repo)
  - sandbox smoke test (Tier 1 bwrap) OR docker_precondition_check (Tier 2)
  - hook precheck (refuse to launch if disableAllHooks=true anywhere)
  - cache env-var exports (relocate ~/.cache into project-local)
  - cleanup_children trap via core.claude._kill_group
  - tee to logs/grind-<ts>.log; terminal stays quiet (ortus-6q8v invariant)

New behavior:
  - --orphan-policy={warn,revert,escalate} (default warn)
  - --idle-sleep N seconds slept on no-change iterations (default 60)
  - --tasks N caps `tasks_completed` (count of bd-state-verified closes)
  - --iterations N caps the number of subprocess spawns
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import typer

from ortus.core import cache, hooks, output, sandbox
from ortus.core.bd import BdClient
from ortus.core.claude import ClaudeRunner
from ortus.core.git import GitClient
from ortus.core.grind_logic import (
    FlockBusy,
    build_condition,
    grind_flock,
)
from ortus.core.grind_loop import (
    DEFAULT_INTEGRATION_BRANCH,
    EXCLUDED_LABELS,
    BranchDisposition,
    OrphanPolicy,
    StateSnapshot,
    apply_orphan_policy,
    classify_branch_state,
    compute_delta,
    inject_issue,
    queue_drained,
    read_work_issue_condition,
    select_ready_issue,
)
from ortus.core.repo import resolve_repo


def _make_runner() -> ClaudeRunner:
    """Indirection so tests can swap in a fake claude binary."""
    return ClaudeRunner()


def _make_bd(repo: Path) -> BdClient:
    """Indirection so tests can swap in a stub bd client."""
    return BdClient(repo=repo)


def _make_git(repo: Path) -> GitClient:
    """Indirection so tests can swap in a stub git client."""
    return GitClient(repo=repo)


def _enforce_branch_discipline(
    git: GitClient,
    integration_branch: str,
    write_log: Callable[[str], None],
    *,
    phase: str,
) -> None:
    """Pin the working tree to the integration branch and keep origin current.

    Called at the top of every iteration AND after each close so a closed
    issue's commit always lands on origin/<integration> (deployable), never
    stranded on a feature branch (ortus-6fu6). No-op when the repo isn't
    git-backed. Raises typer.Exit(1) on a stranded-work HALT so the loop stops
    loudly instead of silently piling work onto an off-deploy-path branch.

    `phase` is a short tag ('startup' / 'pre-iter' / 'post-close') for the log.
    """
    if not git.is_git_repo():
        return

    # A repo with no commits yet (unborn branch, e.g. right after `ortus init`)
    # has nothing stranded and no commit to push; branch discipline is moot.
    # Skipping here also avoids misreading the unborn branch — where
    # `git rev-parse --abbrev-ref HEAD` fails and current_branch() is "" — as a
    # detached HEAD and halting the loop before any work has been done.
    if not git.has_commits():
        write_log(f"branch-guard [{phase}]: repo has no commits yet; skipping")
        return

    decision = classify_branch_state(git.branch_state(integration_branch))
    disp = decision.disposition

    if disp is BranchDisposition.OK:
        write_log(f"branch-guard [{phase}]: {decision.reason}")
        return

    if disp is BranchDisposition.PUSH:
        if not git.has_remote():
            write_log(
                f"branch-guard [{phase}]: {decision.reason} "
                "(no remote configured; nothing to push)"
            )
            return
        pushed = git.push(integration_branch)
        write_log(
            f"branch-guard [{phase}]: {decision.reason} "
            f"({'pushed' if pushed else 'PUSH FAILED'})"
        )
        if not pushed:
            output.error(
                f"grind: push of {integration_branch} to origin failed; the "
                "closed work is NOT on origin yet",
                hint="pull --rebase and push manually, then re-run grind",
            )
        return

    if disp is BranchDisposition.REASSERT:
        ok = git.checkout(integration_branch)
        write_log(
            f"branch-guard [{phase}]: {decision.reason} "
            f"({'re-checked out' if ok else 'CHECKOUT FAILED'})"
        )
        if not ok:
            output.error(
                f"grind: could not re-checkout {integration_branch}",
                hint="resolve the working tree state manually, then re-run grind",
            )
            raise typer.Exit(code=1)
        return

    # HALT — stranded work or detached HEAD. Surface loudly and stop.
    write_log(f"branch-guard [{phase}]: HALT — {decision.reason}")
    output.error(
        f"grind halted (branch discipline): {decision.reason}",
        hint=(
            f"a closed issue must land on origin/{integration_branch} to be "
            "deployable; grind will not continue while work is stranded"
        ),
    )
    raise typer.Exit(code=1)


def _log_path(repo: Path) -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = repo / "logs" / f"grind-{ts}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return log


def _snapshot(bd: BdClient) -> StateSnapshot:
    """Read all four bd state values needed by the outer loop in one shot.

    `open` and `in_progress` are counted with EXCLUDED_LABELS applied so
    human-flagged issues don't keep the queue artificially non-empty;
    `closed` is reported verbatim (historical, never gates loop control).
    """
    return StateSnapshot.from_counts(
        closed=bd.count_by_status("closed"),
        in_progress=bd.count_by_status("in_progress", exclude_labels=EXCLUDED_LABELS),
        open=bd.count_by_status("open", exclude_labels=EXCLUDED_LABELS),
        in_progress_ids=bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS),
    )


def _legacy_prompt(custom_condition: str) -> str:
    """The per-subprocess /goal prompt for the legacy `--condition` path.

    When the operator pins a custom condition we leave SELECTION to the worker
    (verbatim, every iteration) for backwards compatibility. The default path
    instead has the harness select+claim and inject the issue per iteration
    (see `_compose_work_prompt`), which is composed live inside the loop.
    """
    return f"/goal {custom_condition}"


def _compose_work_prompt(template: str, issue: dict) -> str:
    """Fill the work-issue template with the harness-claimed issue's id +
    details and prefix the /goal directive. The worker is TOLD which issue to
    work rather than choosing/transcribing it — eliminating the hallucinated-id
    failure mode at the source."""
    return f"/goal {inject_issue(template, issue)}"


def _log_writer(log_path: Path) -> Callable[[str], None]:
    """Tee-style logger: write a timestamped line to log_path; terminal stays quiet."""

    def _write(msg: str) -> None:
        line = f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)

    return _write


def grind(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    tasks: int = typer.Option(
        0, "--tasks", help="Stop after N bd-state-verified closes (0 = drain queue)."
    ),
    iterations: int = typer.Option(
        0, "--iterations", help="Stop after N claude subprocess spawns (0 = unlimited)."
    ),
    condition: Optional[str] = typer.Option(
        None,
        "-c",
        "--condition",
        help="Custom per-iteration /goal condition (overrides bundled close-one.txt).",
    ),
    orphan_policy: OrphanPolicy = typer.Option(
        OrphanPolicy.REVERT,
        "--orphan-policy",
        help="How to handle claimed-but-unclosed issues: warn|revert|escalate.",
        case_sensitive=False,
    ),
    idle_sleep: int = typer.Option(
        60,
        "--idle-sleep",
        help="Seconds to sleep after a no-change iteration (suspected evaluator false-positive).",
    ),
    worker_timeout: int = typer.Option(
        1800,
        "--worker-timeout",
        help=(
            "Hard cap (secs) on a single iteration's worker subprocess. On exceed, "
            "SIGTERM then SIGKILL the worker's whole process group (killing any child "
            "bd/dolt/build processes and releasing their locks), then run the normal "
            "post-iteration recovery (bd-state delta + orphan-policy). 0 disables the "
            "watchdog (workers may then hang the loop indefinitely)."
        ),
    ),
    integration_branch: str = typer.Option(
        DEFAULT_INTEGRATION_BRANCH,
        "--integration-branch",
        help=(
            "Branch grind pins the working tree to. A closed issue's commit must "
            "land on origin/<branch> to be deployable; grind re-asserts this branch "
            "each iteration and halts loudly if a worker strands work on a side "
            "branch instead of silently leaving origin stale."
        ),
    ),
    fast: bool = typer.Option(False, "--fast", help="Use claude --fast (premium output)."),
    docker: bool = typer.Option(
        False, "--docker", help="Run claude inside docker sandbox instead of bwrap."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print resolved flags + composed per-iteration prompt; do not spawn claude.",
    ),
) -> None:
    """Drive the bd queue via a subprocess-per-task /goal loop (ortus-3ico)."""
    target = resolve_repo(repo)

    # Two per-iteration prompt shapes:
    #   - default (no --condition): the harness selects + claims the next ready
    #     issue itself and injects its exact id + details into the work-issue
    #     template per iteration, so the worker is TOLD which issue to work and
    #     never runs `bd ready` or transcribes a hash-like id (the
    #     id-hallucination wedge this loop exists to prevent).
    #   - legacy (--condition set): the worker self-selects, verbatim every
    #     iteration, for one-off operator invocations / queue-zero conditions.
    # build_condition() is preserved for the legacy queue-zero shape so that
    # `-c "$(cat queue-zero.txt)"` continues to work; the outer loop never
    # calls it.
    _ = build_condition  # re-export retained for downstream tooling/tests

    harness_select = condition is None
    work_template = read_work_issue_condition() if harness_select else ""

    if dry_run:
        output.info(f"repo:           {target}")
        output.info(f"tasks:          {tasks}")
        output.info(f"iterations:     {iterations}")
        output.info(f"orphan-policy:  {orphan_policy.value}")
        output.info(f"integration:    {integration_branch}")
        output.info(f"idle-sleep:     {idle_sleep}s")
        output.info(
            f"worker-timeout: {worker_timeout}s" if worker_timeout > 0 else "worker-timeout: off"
        )
        output.info(f"fast:           {fast}")
        output.info(f"docker:         {docker}")
        output.info(f"select:         {'harness (per-iteration claim)' if harness_select else 'worker (legacy --condition)'}")
        output.info("--- per-iteration prompt ---")
        if harness_select:
            output.info(
                "/goal " + work_template.rstrip()
                + "\n(the harness fills <ISSUE_ID>/<ISSUE_DETAILS> per iteration "
                "from the next ready issue it claims.)"
            )
        else:
            output.info(_legacy_prompt(condition))
        return

    # Phase 0 — sandbox precondition (Tier 1 native vs Tier 2 docker).
    try:
        if docker:
            sandbox.docker_precondition_check()
        else:
            sandbox.smoke_test()
    except sandbox.SandboxUnavailable as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)

    # Phase 1 — hook precheck (must run BEFORE any claude spawn).
    try:
        hooks.check_hooks_enabled(target)
    except hooks.HookConflictError as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)

    # Phase 2 — flock so two grinds can't race for the same repo.
    try:
        with grind_flock(target):
            log = _log_path(target)
            write_log = _log_writer(log)
            write_log("=== ortus grind started (subprocess-per-task shape) ===")
            output.progress("grind", f"starting; log → {log.relative_to(target)}")

            bd = _make_bd(target)
            git = _make_git(target)
            # Re-assert branch discipline before anything else: a stray branch
            # left by a prior crashed grind (or a manual checkout) is caught
            # here and either re-checked-out or halted on, so we never start
            # spawning workers on top of stranded work (ortus-6fu6).
            _enforce_branch_discipline(
                git, integration_branch, write_log, phase="startup"
            )
            initial_snapshot = _snapshot(bd)
            write_log(
                f"initial state: open={initial_snapshot.open} "
                f"in_progress={initial_snapshot.in_progress} "
                f"closed={initial_snapshot.closed}"
            )
            output.progress(
                "grind",
                f"initial state: open={initial_snapshot.open} "
                f"in_progress={initial_snapshot.in_progress} "
                f"closed={initial_snapshot.closed}",
            )

            # We hold the exclusive flock, so any in_progress issue at this
            # point is a cross-restart orphan: a prior grind claimed it and
            # was killed before closing. Per-iteration orphan detection
            # (compute_delta on the before/after diff) can never see these
            # because they sit in `before.in_progress_ids` and get subtracted
            # out of every later delta.
            if initial_snapshot.in_progress_ids:
                orphan_ids = initial_snapshot.in_progress_ids
                write_log(
                    f"startup orphan sweep: {len(orphan_ids)} "
                    f"orphan(s) from prior grind: {sorted(orphan_ids)}"
                )
                action = apply_orphan_policy(
                    orphan_policy,
                    orphan_ids,
                    revert_fn=lambda i: bd.update_status(i, "open"),
                    escalate_fn=lambda i: bd.add_label(i, "human"),
                )
                for line in action.actions_taken:
                    write_log(f"  orphan-policy: {line}")
                # Re-snapshot so the queue_drained check below — and the
                # loop's first `before` — see post-sweep state (revert
                # moves in_progress → open; escalate trims it from the
                # human-excluded counts).
                initial_snapshot = _snapshot(bd)
                write_log(
                    f"post-sweep state: open={initial_snapshot.open} "
                    f"in_progress={initial_snapshot.in_progress} "
                    f"closed={initial_snapshot.closed}"
                )

            if queue_drained(initial_snapshot):
                write_log("queue already drained; nothing to do.")
                output.progress("grind", "queue already drained; nothing to do.")
                return

            # Phase 3 — cache env vars (relocate ~/.cache into project-local).
            cache.ensure_cache_dirs(target)
            cache_env = cache.env_overrides(target)
            runner = _make_runner()
            runner.extra_env.update(cache_env)

            tasks_completed = 0
            iters_run = 0

            while True:
                before = _snapshot(bd)
                if queue_drained(before):
                    write_log(
                        f"queue drained; exiting outer loop. tasks_completed={tasks_completed}"
                    )
                    break

                # Re-assert the working tree onto the integration branch before
                # spawning the worker, so it commits onto main (not whatever a
                # previous worker drifted onto). Halts loudly on stranded work
                # (ortus-6fu6).
                _enforce_branch_discipline(
                    git, integration_branch, write_log, phase="pre-iter"
                )

                # Default path: select + claim the next ready issue IN-HARNESS,
                # then inject its exact id + details into the per-iteration
                # prompt. The claim happens AFTER the `before` snapshot above so
                # the existing orphan detection (after.in_progress_ids -
                # before.in_progress_ids) still sees this iteration's claim as
                # fresh — a worker that fails to close it lands in the orphan
                # branch and gets the orphan-policy treatment, unchanged.
                if harness_select:
                    try:
                        ready = bd.list_ready(exclude_labels=EXCLUDED_LABELS)
                    except Exception as exc:  # bd hiccup: don't crash the loop
                        write_log(f"iter prep: bd ready failed ({exc}); idle-sleeping")
                        if idle_sleep > 0:
                            time.sleep(idle_sleep)
                        continue
                    target_issue = select_ready_issue(ready)
                    if target_issue is None:
                        # Queue is non-empty (not drained) but nothing is ready —
                        # everything left is blocked or human-flagged. We hold the
                        # flock, so no other actor will unblock it; stop rather
                        # than spin spawning workers that have nothing to do.
                        write_log(
                            "no ready issue to claim (queue blocked or human-only); "
                            f"exiting outer loop. tasks_completed={tasks_completed}"
                        )
                        break
                    issue_id = target_issue["id"]
                    try:
                        bd.update_status(issue_id, "in_progress")
                    except Exception as exc:
                        write_log(
                            f"iter prep: claim of {issue_id} failed ({exc}); idle-sleeping"
                        )
                        if idle_sleep > 0:
                            time.sleep(idle_sleep)
                        continue
                    iteration_prompt = _compose_work_prompt(work_template, target_issue)
                    write_log(
                        f"iter {iters_run + 1}: harness selected+claimed {issue_id}; "
                        "injected into /goal prompt"
                    )
                else:
                    iteration_prompt = _legacy_prompt(condition)

                iters_run += 1
                write_log(f"iter {iters_run}: spawning claude (work-issue /goal)")
                # A stuck-but-alive worker would otherwise block the entire
                # loop forever (only a human kill recovers it). --worker-timeout
                # hard-caps the iteration: on exceed the runner SIGTERM/SIGKILLs
                # the worker's process group, we log it distinctly, and fall
                # through to the SAME post-iteration recovery as a clean exit —
                # bd state is ground truth, so a worker that closed its issue
                # then hung still counts, and a claimed-but-unclosed issue still
                # gets the orphan-policy treatment.
                try:
                    rc = runner.run(
                        iteration_prompt,
                        repo=target,
                        log_path=log,
                        fast=fast,
                        timeout=(worker_timeout if worker_timeout > 0 else None),
                    )
                except subprocess.TimeoutExpired:
                    rc = 143  # 128 + SIGTERM; group was SIGTERM'd then SIGKILL'd
                    write_log(
                        f"iter {iters_run}: worker TIMEOUT after {worker_timeout}s, "
                        f"killed (rc={rc})"
                    )

                after = _snapshot(bd)
                delta = compute_delta(before, after)

                if delta.closed_one_or_more:
                    tasks_completed += delta.closed_delta
                    write_log(
                        f"iter {iters_run}: closed +{delta.closed_delta} "
                        f"(tasks_completed={tasks_completed}, rc={rc})"
                    )
                    # Verify the close actually reached origin/<integration>.
                    # If the worker committed onto main but didn't push, push
                    # it; if it drifted onto a side branch and committed the
                    # close there, halt loudly — a "closed" issue that isn't on
                    # origin/<integration> is not deployable (ortus-6fu6).
                    _enforce_branch_discipline(
                        git, integration_branch, write_log, phase="post-close"
                    )
                elif delta.is_orphan:
                    write_log(
                        f"iter {iters_run}: WARN orphan claim "
                        f"(in_progress +{delta.in_progress_delta}, ids={sorted(delta.orphan_ids)}, rc={rc})"
                    )
                    action = apply_orphan_policy(
                        orphan_policy,
                        delta.orphan_ids,
                        revert_fn=lambda i: bd.update_status(i, "open"),
                        escalate_fn=lambda i: bd.add_label(i, "human"),
                    )
                    for line in action.actions_taken:
                        write_log(f"  orphan-policy: {line}")
                else:
                    write_log(
                        f"iter {iters_run}: WARN no bd-state change (rc={rc})"
                    )

                # Cap checks BEFORE the idle-sleep so we don't burn idle time
                # when the loop is about to exit anyway.
                if tasks > 0 and tasks_completed >= tasks:
                    write_log(
                        f"--tasks cap reached: {tasks_completed}/{tasks}; exiting outer loop"
                    )
                    break
                if iterations > 0 and iters_run >= iterations:
                    write_log(
                        f"--iterations cap reached: {iters_run}/{iterations}; exiting outer loop"
                    )
                    break

                if delta.is_no_change and idle_sleep > 0:
                    write_log(f"  idle-sleep {idle_sleep}s before retry")
                    time.sleep(idle_sleep)

            final_snapshot = _snapshot(bd)
            write_log(
                f"=== ortus grind ended; closed {tasks_completed} "
                f"(open: {initial_snapshot.open} → {final_snapshot.open}, "
                f"in_progress: {final_snapshot.in_progress}, "
                f"iters_run={iters_run}) ==="
            )
            output.progress(
                "grind",
                f"done; closed {tasks_completed} this session "
                f"(open: {initial_snapshot.open} → {final_snapshot.open}, "
                f"in_progress: {final_snapshot.in_progress})",
            )
    except FlockBusy as exc:
        output.error(str(exc), hint="another `ortus grind` is already running here")
        raise typer.Exit(code=1)
