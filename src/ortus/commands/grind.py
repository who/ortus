"""ortus grind <repo> — subprocess-per-task outer loop (ortus-3ico pivot).

Each iteration spawns a fresh backend worker subprocess. Claude receives a
narrow `/goal`; Codex receives the same logical task as a plain `codex exec`
prompt.
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
  - process-group cleanup via the shared runner implementation
  - tee to logs/grind-<ts>.log; terminal stays quiet (ortus-6q8v invariant)

New behavior:
  - --orphan-policy={warn,revert,escalate} (default warn)
  - --idle-sleep N seconds slept on no-change iterations (default 60)
  - --tasks N caps `tasks_completed` (count of bd-state-verified closes)
  - --iterations N caps the number of subprocess spawns
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import typer

from ortus.core import cache, hooks, output, sandbox
from ortus.core.agent import BackendError, compose_worker_prompt, make_runner, resolve_backend
from ortus.core.bd import BdClient
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import (
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphUnavailable,
    append_normalized,
    parse_transcript,
    phase_contract,
    require_handshake,
)
from ortus.core.config import load_config
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


def _make_runner(backend: str = "claude") -> ClaudeRunner:
    """Indirection so tests can swap in a fake backend runner."""
    return make_runner(backend)  # type: ignore[arg-type]


def _make_bd(repo: Path) -> BdClient:
    """Indirection so tests can swap in a stub bd client."""
    return BdClient(repo=repo)


def _make_git(repo: Path) -> GitClient:
    """Indirection so tests can swap in a stub git client."""
    return GitClient(repo=repo)


def _make_codegraph() -> CodeGraphAdapter:
    """Indirection for lifecycle tests with a deterministic fake adapter."""
    return CodeGraphAdapter()


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


def _legacy_prompt(custom_condition: str, backend: str = "claude") -> str:
    """The per-subprocess /goal prompt for the legacy `--condition` path.

    When the operator pins a custom condition we leave SELECTION to the worker
    (verbatim, every iteration) for backwards compatibility. The default path
    instead has the harness select+claim and inject the issue per iteration
    (see `_compose_work_prompt`), which is composed live inside the loop.
    """
    return compose_worker_prompt(backend, custom_condition)  # type: ignore[arg-type]


_CLAUDE_GOAL_CONDITION_LIMIT = 4_000


def _compose_work_prompt(
    template: str,
    issue: dict,
    backend: str = "claude",
    *,
    phase_instruction: str = "",
    phase_contract_text: str = "",
) -> str:
    """Build one backend-appropriate prompt for a harness-claimed issue.

    Codex receives the complete issue packet because its plain ``exec`` prompt
    has no slash-command condition limit. Claude's ``/goal`` condition is
    capped at 4,000 characters, so it receives the exact claimed id and loads
    the authoritative, deliberately thorough packet from bd itself.
    """
    issue_id = str(issue.get("id") or "")
    if not issue_id:
        raise ValueError("cannot compose a worker prompt for an issue with no id")

    if backend == "codex":
        task = inject_issue(template, issue)
        if phase_instruction:
            task = phase_instruction.rstrip() + "\n\n" + task
        task += phase_contract_text
        return compose_worker_prompt("codex", task)

    task = (
        f"Work bd issue {issue_id}. The grind harness already selected and claimed "
        "this exact issue. Do not run bd ready, select another issue, or operate on "
        f"any other id. First run `bd show {issue_id} --json`; its description, "
        "design, acceptance criteria, and notes are the authoritative implementation "
        "packet. Follow that packet and the repository instructions. Run the required "
        "checks, use only the exact claimed id in bd commands, and do not invoke another "
        "queue orchestrator. If a human decision is required, flag this exact issue and "
        "stop. Otherwise complete only this issue, close it when the active phase permits, "
        "commit and push when repository instructions require it, then end the session."
    )
    if phase_instruction:
        task += "\n\n" + phase_instruction.rstrip()
    task += phase_contract_text
    if len(task) > _CLAUDE_GOAL_CONDITION_LIMIT:
        raise BackendError(
            "internal Claude /goal condition exceeds the 4,000-character limit "
            f"({len(task)} characters)"
        )
    return compose_worker_prompt("claude", task)


def _claude_goal_rejection(
    log_path: Path, *, start_offset: int
) -> str | None:
    """Return a zero-turn Claude goal-condition rejection from a log slice."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(start_offset)
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        result = event.get("result")
        if event.get("num_turns") != 0 or not isinstance(result, str):
            continue
        lowered = result.lower()
        if "goal condition" in lowered and any(
            marker in lowered for marker in ("limited", "invalid", "exceed")
        ):
            return result.strip()
    return None


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
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="Agent backend (claude|codex); overrides ORTUS_BACKEND and .ortusrc.",
    ),
    codegraph: Optional[CodeGraphMode] = typer.Option(
        None,
        "--codegraph",
        help="CodeGraph policy: off|auto|required (defaults from .ortusrc).",
        case_sensitive=False,
    ),
) -> None:
    """Drive the bd queue via a subprocess-per-task /goal loop (ortus-3ico)."""
    target = resolve_repo(repo)
    try:
        resolved_backend = resolve_backend(backend, repo=target)
    except BackendError as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)

    configured_mode = load_config(repo=target).get("codegraph", "auto")
    try:
        codegraph_mode = codegraph or CodeGraphMode(configured_mode)
    except ValueError:
        output.error(
            f"invalid CodeGraph mode {configured_mode!r}; expected off, auto, or required"
        )
        raise typer.Exit(code=1)
    codegraph_adapter = _make_codegraph()
    if not dry_run:
        output.progress("grind", f"CodeGraph probe (mode={codegraph_mode.value})")
    try:
        codegraph_probe = codegraph_adapter.probe(target, codegraph_mode)
    except CodeGraphUnavailable as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
    if not dry_run:
        if codegraph_mode is CodeGraphMode.OFF:
            output.progress("grind", "CodeGraph disabled by policy")
        elif codegraph_probe.available:
            output.progress("grind", "CodeGraph activated for issue transactions")
        else:
            output.progress("grind", f"CodeGraph fallback: {codegraph_probe.reason}")

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
        output.info(f"backend:        {resolved_backend}")
        output.info(f"codegraph:      {codegraph_mode.value}")
        output.info(f"select:         {'harness (per-iteration claim)' if harness_select else 'worker (legacy --condition)'}")
        output.info("--- per-iteration prompt ---")
        if harness_select:
            output.info(
                _compose_work_prompt(
                    work_template,
                    {"id": "<ISSUE_ID>", "title": "<ISSUE_DETAILS>"},
                    resolved_backend,
                )
                + "\n(the harness fills <ISSUE_ID>/<ISSUE_DETAILS> per iteration "
                "from the next ready issue it claims.)"
            )
        else:
            output.info(_legacy_prompt(condition, resolved_backend))
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
    if resolved_backend == "claude":
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
            write_log(
                "=== ortus grind started "
                f"(subprocess-per-task shape; backend={resolved_backend}) ==="
            )
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
            # Preserve the zero-argument seam used by existing Claude test and
            # plugin overrides. Codex is the only branch that needs an explicit
            # selector here.
            runner = (
                _make_runner() if resolved_backend == "claude" else _make_runner("codex")
            )
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

                # Codex runs under workspace-write, which intentionally keeps
                # .git metadata read-only. Ortus therefore owns the commit
                # after a verified close. Requiring a clean tree beforehand
                # makes the later `git add -A` safe: it cannot sweep unrelated
                # operator work into the worker's commit.
                if (
                    resolved_backend == "codex"
                    and git.is_git_repo()
                    and not git.is_clean()
                ):
                    write_log(
                        "pre-iter: HALT — Codex backend requires a clean git "
                        "worktree before it can create an iteration commit"
                    )
                    output.error(
                        "grind: Codex backend requires a clean git worktree",
                        hint="commit or stash existing changes, then re-run grind",
                    )
                    raise typer.Exit(code=1)

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
                    implementation_instruction = (
                        "IMPLEMENTATION PHASE ONLY. Make candidate edits and run targeted "
                        "checks, but do not add the final verification comment and do not "
                        "close the issue; a fresh verifier follows."
                    )
                    try:
                        iteration_prompt = _compose_work_prompt(
                            work_template,
                            target_issue,
                            resolved_backend,
                            phase_instruction=implementation_instruction,
                            phase_contract_text=phase_contract(
                                CodeGraphPhase.IMPLEMENTATION, codegraph_probe
                            ),
                        )
                    except BackendError as exc:
                        bd.update_status(issue_id, "open")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                    write_log(
                        f"iter {iters_run + 1}: harness selected+claimed {issue_id}; "
                        f"injected into {resolved_backend} worker prompt"
                    )
                else:
                    iteration_prompt = _legacy_prompt(condition, resolved_backend)

                iters_run += 1
                write_log(
                    f"iter {iters_run}: spawning {resolved_backend} "
                    "(single-issue worker)"
                )
                # A stuck-but-alive worker would otherwise block the entire
                # loop forever (only a human kill recovers it). --worker-timeout
                # hard-caps the iteration: on exceed the runner SIGTERM/SIGKILLs
                # the worker's process group, we log it distinctly, and fall
                # through to the SAME post-iteration recovery as a clean exit —
                # bd state is ground truth, so a worker that closed its issue
                # then hung still counts, and a claimed-but-unclosed issue still
                # gets the orphan-policy treatment.
                try:
                    phase_offset = log.stat().st_size if log.exists() else 0
                    output.progress(
                        "grind",
                        "implementation CodeGraph "
                        + ("engaged" if codegraph_probe.available else "fallback active"),
                    )
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

                if resolved_backend == "claude":
                    rejection = _claude_goal_rejection(
                        log, start_offset=phase_offset
                    )
                    if rejection is not None:
                        if harness_select:
                            bd.update_status(issue_id, "open")
                        write_log(
                            f"iter {iters_run}: HALT — Claude rejected /goal before "
                            f"running a worker turn: {rejection}"
                        )
                        output.error(
                            "grind: Claude rejected the /goal condition before worker work",
                            hint=rejection,
                        )
                        raise typer.Exit(code=1)

                implementation_summary = parse_transcript(
                    log,
                    phase=CodeGraphPhase.IMPLEMENTATION,
                    probe=codegraph_probe,
                    start_offset=phase_offset,
                )
                append_normalized(log, implementation_summary)
                write_log(
                    f"CodeGraph implementation summary: queries={len(implementation_summary.events)} "
                    f"fallbacks={implementation_summary.fallbacks or 'none'}"
                )
                try:
                    require_handshake(implementation_summary)
                except CodeGraphUnavailable as exc:
                    if harness_select:
                        bd.update_status(issue_id, "open")
                    output.error(str(exc))
                    raise typer.Exit(code=1)

                # Candidate edits are indexed by the parent before a fresh
                # verifier starts. Refresh failure is blocking only in required
                # mode (auto records the stale fallback and continues).
                output.progress("grind", "refreshing CodeGraph index before verification")
                freshness, sync_ms = codegraph_adapter.refresh(target, codegraph_probe)
                implementation_summary.freshness = freshness
                implementation_summary.sync_duration_ms = sync_ms
                write_log(
                    f"CodeGraph refresh: result={freshness} duration_ms={sync_ms}"
                )
                if freshness == "sync-failed" and codegraph_mode is CodeGraphMode.REQUIRED:
                    if harness_select:
                        bd.update_status(issue_id, "open")
                    output.error(
                        "CodeGraph required but index refresh failed before verification"
                    )
                    raise typer.Exit(code=1)

                mid = _snapshot(bd)
                if harness_select and issue_id in mid.in_progress_ids:
                    verification_instruction = (
                        "FRESH VERIFICATION PHASE. Do not trust the implementation worker's "
                        "claims. Inspect the candidate diff and issue independently, run the "
                        "changed-surface tests, add a thorough bd comment, and close only if "
                        "every acceptance criterion passes."
                    )
                    try:
                        verifier_prompt = _compose_work_prompt(
                            work_template,
                            target_issue,
                            resolved_backend,
                            phase_instruction=verification_instruction,
                            phase_contract_text=phase_contract(
                                CodeGraphPhase.VERIFICATION, codegraph_probe
                            ),
                        )
                    except BackendError as exc:
                        bd.update_status(issue_id, "open")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                    verify_offset = log.stat().st_size if log.exists() else 0
                    output.progress(
                        "grind",
                        "verification CodeGraph "
                        + ("engaged" if codegraph_probe.available else "fallback active"),
                    )
                    try:
                        rc = runner.run(
                            verifier_prompt,
                            repo=target,
                            log_path=log,
                            fast=fast,
                            timeout=(worker_timeout if worker_timeout > 0 else None),
                        )
                    except subprocess.TimeoutExpired:
                        rc = 143
                        write_log(
                            f"iter {iters_run}: verifier TIMEOUT after {worker_timeout}s"
                        )
                    if resolved_backend == "claude":
                        rejection = _claude_goal_rejection(
                            log, start_offset=verify_offset
                        )
                        if rejection is not None:
                            bd.update_status(issue_id, "open")
                            write_log(
                                f"iter {iters_run}: HALT — Claude rejected verifier /goal "
                                f"before running a worker turn: {rejection}"
                            )
                            output.error(
                                "grind: Claude rejected the verifier /goal condition "
                                "before worker work",
                                hint=rejection,
                            )
                            raise typer.Exit(code=1)
                    verification_summary = parse_transcript(
                        log,
                        phase=CodeGraphPhase.VERIFICATION,
                        probe=codegraph_probe,
                        start_offset=verify_offset,
                    )
                    verification_summary.freshness = freshness
                    verification_summary.sync_duration_ms = sync_ms
                    append_normalized(log, verification_summary)
                    try:
                        require_handshake(verification_summary)
                    except CodeGraphUnavailable as exc:
                        bd.update_status(issue_id, "open")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                else:
                    # Compatibility/safety: if an implementation worker closed
                    # despite the phase contract, still leave durable evidence.
                    verification_summary = implementation_summary
                    verification_summary.phase = CodeGraphPhase.VERIFICATION.value

                if harness_select:
                    bd.add_comment(issue_id, verification_summary.report())
                output.progress(
                    "grind",
                    f"CodeGraph phase summary: {len(verification_summary.events)} queries, "
                    f"freshness={freshness}",
                )

                after = _snapshot(bd)
                delta = compute_delta(before, after)

                if delta.closed_one_or_more:
                    tasks_completed += delta.closed_delta
                    write_log(
                        f"iter {iters_run}: closed +{delta.closed_delta} "
                        f"(tasks_completed={tasks_completed}, rc={rc})"
                    )
                    if resolved_backend == "codex" and git.is_git_repo():
                        commit_subject = (
                            f"{issue_id}: complete Codex grind task"
                            if harness_select
                            else "ortus: complete Codex grind iteration"
                        )
                        if not git.commit_all(commit_subject):
                            write_log(
                                f"iter {iters_run}: HALT — outer Codex commit failed"
                            )
                            output.error(
                                "grind: Codex completed the issue but the outer "
                                "git commit failed",
                                hint="inspect the worktree and commit the completed work manually",
                            )
                            raise typer.Exit(code=1)
                        write_log(
                            f"iter {iters_run}: outer Codex commit completed"
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
