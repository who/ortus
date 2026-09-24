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
  - tee to logs/grind-<ts>.log; worker transcripts never reach the terminal
    (ortus-6q8v invariant, narrowed by ortus-kawu: the console DOES narrate
    per-issue milestones — claim, verdict, corrections, landings — while
    healthy CodeGraph plumbing narrates to the log only)

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
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from uuid import UUID, uuid4

import typer

from ortus.core import cache, hooks, output, sandbox
from ortus.core.agent import (
    BackendError,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
)
from ortus.core.bd import BdClient, BdError
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import (
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphUnavailable,
    LEAVE_OPEN_FOR_VERIFICATION,
    append_normalized,
    parse_transcript,
    phase_contract,
    require_handshake,
)
from ortus.core.prompt_audit import audit_enabled, audit_note
from ortus.core.prompt_prefix import stable_prefix_enabled, stable_prefix_note
from ortus.core.prompts import resolve_named_prompt
from ortus.core.config import (
    Config,
    DEFAULT_MERGE_GATE_TIMEOUT,
    DEFAULT_VERIFICATION_MODE,
    VERIFICATION_PROTOTYPE,
    load_config,
)
from ortus.core.profiles import Phase, ProfileError
from ortus.core.readiness import (
    READINESS_MEMORY_KEY,
    ReadinessReport,
)
from ortus.core.git import GitClient
from ortus.core.grind_logic import (
    FlockBusy,
    build_condition,
    grind_flock,
)
from ortus.core.init_render import (
    LANGUAGE_MARKERS,
    LINTER_DEFAULTS,
    PROTOTYPE_LINT_COMMANDS,
    PROTOTYPE_SYNTAX_COMMANDS,
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
    epic_is_exhausted,
    queue_drained,
    read_work_issue_condition,
    select_ready_issue,
)
from ortus.core.lifecycle import ISSUE_OPEN
from ortus.core.local_backend import (
    LOCAL_TABLE_BACKENDS,
    LocalServerError,
    OpenCodeBinaryError,
    load_local_config,
    probe_models,
    resolve_opencode_binary,
)
from ortus.core.repo import resolve_repo
from ortus.core.pin_skew import tag_pin_skew_claims
from ortus.core.worker_failure import classify_worker_window, failure_log_line
from ortus.core.judge import (
    GateAction, JudgeAnswers, JudgeConfig, JudgeMode, JudgeState, parse_judge_config,
)
from ortus.core.judge_readiness import evaluate_readiness, readiness_context
from ortus.core.judge_claim import BoundIssue, prepare_bound_issue, validate_bound_goal
from ortus.core.judge_hooks import HookRun, check_pre_tool
from ortus.core.judge_log import (
    DecisionEvent, JudgeLogError, ModelRouteEvent, OutcomeEvent, OutcomeStatus,
    elapsed_ms, write_decision, write_model_route, write_outcome, write_shadow_outcome,
)
from ortus.core.judge_policy import decide_pre_turn
from ortus.core.judge_packs import CRITERIA_VERSION, criteria_hash
from ortus.core.judge_post import (
    OutcomeVerdict, WorkerOutcome, apply_outcome, evaluate_outcome,
)
from ortus.core.judge_stuck import (
    WEDGED_WINDOW_THRESHOLD, StuckAction, StuckDecision, baseline_stuck_action,
    decide_stuck_claim, log_stuck_decision,
)
from ortus.core.judge_routing import (
    ExecutionBundle, RouteOverrides, RoutePlan, RoutePreparationError,
    baseline_implement_profile, jev_router_enabled, plan_routes, prepare_route,
    route_implement_profile,
)
from ortus.core.judge_state import StateError, pack_state
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict, TypeSafeJudge, build_questions


def _shadow_turn(
    *, packet: dict, repo: Path, config: Config, judge_config: JudgeConfig,
    baseline: str, overrides: RouteOverrides, run_id: UUID,
) -> UUID | None:
    """Observe the readiness head without preparing a runner or owning its claim."""
    try:
        plan = plan_routes(config, judge_config, baseline, overrides=overrides)
        packing_config = replace(
            judge_config, routes=tuple(dict.fromkeys((*judge_config.routes, plan.baseline))),
        )
        state = pack_state(
            packet, packing_config, backends_available=plan.available_workers,
        ).state
        started = time.monotonic()
        try:
            verdict = TypeSafeJudge(judge_config).evaluate(state)
        except Exception:
            verdict = JudgeVerdict(failure=JudgeFailure.SERVICE_ERROR)
        decision = decide_pre_turn(
            judge_config, state, verdict, baseline_backend=plan.baseline,
        )
        criteria = build_questions(judge_config, state)
        decision_id = write_decision(repo, judge_config, DecisionEvent(
            run_id=run_id, seat=state.seat, issue_id=packet["id"], phase=state.phase,
            answers=verdict.answers, decision=decision, model=judge_config.model,
            criteria_version=CRITERIA_VERSION,
            criteria_hash=criteria_hash(judge_config, criteria),
            latency_ms=elapsed_ms(started), failure=verdict.failure, usage=verdict.usage,
        ))
        if decision_id is not None:
            _observe_route(
                repo=repo, config=config, judge_config=judge_config, plan=plan,
                state=state, answers=verdict.answers, decision_id=decision_id,
                run_id=run_id,
            )
        return decision_id
    except Exception:
        # Observation failures cannot change baseline execution. Never print
        # exception bodies, which may carry provider or issue text.
        output.progress("grind", "judge shadow observation unavailable; continuing baseline")
        return None


def _observe_route(
    *, repo: Path, config: Config, judge_config: JudgeConfig, plan: RoutePlan,
    state: JudgeState, answers: JudgeAnswers | None, decision_id: UUID,
    run_id: UUID,
) -> None:
    """Record the tier a shadow seat would have run this bead on.

    The router is asked unconditionally here rather than through the flag. A
    shadow rollout is exactly the case where the flag is still off, and a seat
    that logged nothing until someone turned the flag on would only ever have
    evidence for the arm that no longer needs it. Nothing is applied: the
    record carries ``applied=False`` and the worker keeps the pinned profile,
    so an observation can never change what runs.

    A judge that answered nothing is not observed at all. The enforcing path
    logs those beads because its arm has to account for every bead it ran, but
    an observation that records no tier and no vector is not evidence of
    anything and would only dilute the rows that are.

    A failure to observe is not a failure to run. The decision record is
    already written by the time this is reached, and losing a tier line costs
    the shadow seat one row of evidence and the bead nothing.
    """
    if answers is None:
        return
    try:
        backend = plan.execution_backend(plan.baseline)
        route = route_implement_profile(
            config, backend, baseline_implement_profile(config, plan), answers,
            overrides=plan.overrides, enabled=True,
        )
        write_model_route(repo, judge_config, ModelRouteEvent(
            run_id=run_id, decision_id=decision_id, seat=state.seat,
            issue_id=state.issue_id, backend=backend, tier=route.tier.value,
            reason=route.reason.value, model=route.profile.model,
            reasoning_effort=route.profile.reasoning_effort,
            needs_frontier=route.needs_frontier, action_risk=route.action_risk,
            difficulty=route.difficulty, applied=False,
        ))
    except Exception:
        output.progress("grind", "judge shadow route unavailable; continuing baseline")


def _shadow_outcome(
    *, bd: BdClient, repo: Path, config: JudgeConfig, run_id: UUID,
    decision_id: UUID, observed_id: str, before_claims: set[str],
    before_closed: set[str], resumed_id: str | None, worker_ms: float,
) -> None:
    """Attribute only a single observable claim, including a resumed worker."""
    try:
        current = bd.in_progress_ids(exclude_labels=())
        closed = bd.closed_ids()
        candidates = (current - before_claims) | (closed - before_closed)
        if resumed_id and resumed_id in current | closed:
            candidates.add(resumed_id)
        actual = next(iter(candidates)) if len(candidates) == 1 else None
        status = OutcomeStatus.UNKNOWN
        if actual == observed_id:
            status = OutcomeStatus(bd.show(actual).get("status"))
        write_shadow_outcome(repo, config, OutcomeEvent(
            run_id, decision_id, status, worker_ms,
        ), observed_issue_id=observed_id, actual_claimed_id=actual)
    except Exception:
        output.progress("grind", "judge shadow outcome unavailable; continuing baseline")


@dataclass
class _GateTurn:
    """Keep fresh claims recoverable until the worker takes responsibility."""

    bound: BoundIssue
    launched: bool = False
    decision_id: UUID | None = None
    cleaned: bool = False

    def cleanup(self, bd: BdClient) -> None:
        if not self.launched and not self.cleaned:
            try:
                self.bound.release(bd)
                self.cleaned = True
            except Exception:
                raise BackendError(
                    "judge claim cleanup failed; inspect the owned claim"
                ) from None

    def current_issue(self, bd: BdClient) -> dict:
        issue = bd.show(self.bound.issue["id"])
        if (
            issue.get("id") != self.bound.issue["id"]
            or issue.get("status") != "in_progress"
            or issue.get("assignee") != self.bound.assignee
            or "human" in (issue.get("labels") or [])
        ):
            raise BackendError("judge bound claim changed before worker launch")
        return issue

    def record_outcome(
        self, bd: BdClient, repo: Path, config: JudgeConfig,
        run_id: UUID, worker_ms: float,
    ) -> None:
        try:
            observed = OutcomeStatus(bd.show(self.bound.issue["id"]).get("status"))
        except Exception:
            observed = OutcomeStatus.UNKNOWN
        if self.decision_id is not None:
            write_outcome(repo, config, OutcomeEvent(
                run_id, self.decision_id, observed, worker_ms,
            ))


def _gate_turn(
    *,
    bd: BdClient,
    issue_id: str,
    repo: Path,
    config: Config,
    judge_config: JudgeConfig,
    baseline: str,
    overrides: RouteOverrides,
    codegraph_mode: CodeGraphMode,
    adapter: CodeGraphAdapter,
    extra_env: Mapping[str, str],
    goal_template: str,
    cleanup: ExitStack,
    run_id: UUID,
) -> tuple[_GateTurn, ExecutionBundle | None]:
    """Preflight, own, judge and log before a worker may consume the claim."""
    try:
        plan = plan_routes(config, judge_config, baseline, overrides=overrides)
        # Prepare every offered worker before acquiring a claim, including the
        # baseline needed for service fail-open. No route inherits another's setup.
        bundles = {
            route: prepare_route(
                plan, route, repo=repo, config=config, codegraph_mode=codegraph_mode,
                extra_env=extra_env, adapter=adapter,
            )
            for route in plan.available_workers
        }
    except RoutePreparationError:
        bd.add_label(issue_id, "human")
        bd.add_comment(issue_id, "judge pre_turn: human reason=route_preparation_failed")
        output.progress(
            "grind", f"judge requires human handling for {issue_id}: route_preparation_failed"
        )
        raise
    turn = _GateTurn(prepare_bound_issue(
        bd, issue_id, goal_template=goal_template,
    ))
    cleanup.callback(turn.cleanup, bd)
    packet = turn.current_issue(bd)
    # The baseline remains available for fail-open even when not offered to the
    # provider. Question construction still filters through configured routes.
    packing_config = replace(
        judge_config, routes=tuple(dict.fromkeys((*judge_config.routes, plan.baseline))),
    )
    state = pack_state(
        packet, packing_config, backends_available=plan.available_workers,
    ).state
    started = time.monotonic()
    verdict = TypeSafeJudge(judge_config).evaluate(state)
    decision = decide_pre_turn(
        judge_config, state, verdict, baseline_backend=plan.baseline,
    )
    criteria = build_questions(judge_config, state)
    turn.decision_id = write_decision(repo, judge_config, DecisionEvent(
        run_id=run_id, seat=state.seat, issue_id=state.issue_id, phase=state.phase,
        answers=verdict.answers, decision=decision, model=judge_config.model,
        criteria_version=CRITERIA_VERSION,
        criteria_hash=criteria_hash(judge_config, criteria),
        latency_ms=elapsed_ms(started), failure=verdict.failure, usage=verdict.usage,
    ))
    turn.current_issue(bd)
    if decision.action == GateAction.HUMAN:
        bd.add_label(issue_id, "human")
        bd.add_comment(issue_id, f"judge pre_turn: human reason={decision.reason.value}")
        output.progress(
            "grind", f"judge requires human handling for {issue_id}: {decision.reason.value}"
        )
    if decision.action != GateAction.PROCEED:
        turn.cleanup(bd)
        turn.record_outcome(bd, repo, judge_config, run_id, 0)
        return turn, None
    bundle = bundles[decision.backend]
    bundle = _route_model(
        bundle, plan,
        repo=repo, config=config, judge_config=judge_config, state=state,
        answers=verdict.answers, decision_id=turn.decision_id, run_id=run_id,
    )
    bundle.runner.extra_env.update(turn.bound.worker_env(bundle.runner.extra_env))
    return turn, bundle


def _route_model(
    bundle: ExecutionBundle,
    plan: RoutePlan,
    *,
    repo: Path,
    config: Config,
    judge_config: JudgeConfig,
    state: JudgeState,
    answers: JudgeAnswers | None,
    decision_id: UUID | None,
    run_id: UUID,
) -> ExecutionBundle:
    """Let the judge's vectors pick the implementation model for this bead.

    The chosen route's own overrides apply only when it is the baseline, the
    same rule preparation used to resolve its profiles, so a fallback backend
    never inherits a pin meant for another one.
    """
    overrides = plan.overrides if bundle.backend == plan.baseline else RouteOverrides()
    backend = bundle.execution_backend or bundle.backend.value
    route = route_implement_profile(
        config, backend, bundle.implement_profile, answers,
        overrides=overrides, enabled=jev_router_enabled(config),
    )
    if decision_id is not None:
        write_model_route(repo, judge_config, ModelRouteEvent(
            run_id=run_id, decision_id=decision_id, seat=state.seat,
            issue_id=state.issue_id, backend=backend, tier=route.tier.value,
            reason=route.reason.value, model=route.profile.model,
            reasoning_effort=route.profile.reasoning_effort,
            needs_frontier=route.needs_frontier, action_risk=route.action_risk,
            difficulty=route.difficulty, applied=True,
        ))
    return replace(bundle, implement_profile=route.profile)


_TRACKER_EXPORT_PATHS = frozenset(
    {
        ".beads/issues.jsonl",
        ".beads/interactions.jsonl",
    }
)


def _make_runner(backend: str = "claude", *, repo: Path | None = None) -> ClaudeRunner:
    """Indirection so tests can swap in a fake backend runner."""
    return make_runner(backend, repo=repo)  # type: ignore[arg-type]


def _make_bd(repo: Path) -> BdClient:
    """Indirection so tests can swap in a stub bd client."""
    return BdClient(repo=repo)


def _make_git(repo: Path) -> GitClient:
    """Indirection so tests can swap in a stub git client."""
    return GitClient(repo=repo)


def _make_codegraph() -> CodeGraphAdapter:
    """Indirection for lifecycle tests with a deterministic fake adapter."""
    return CodeGraphAdapter()


def _append_handshake(
    log_path: Path,
    phase: CodeGraphPhase,
    *,
    success: bool,
    reason: str | None = None,
) -> None:
    record = {
        "type": "ortus.codegraph",
        "schema": 1,
        "kind": "handshake",
        "phase": phase.value,
        "success": success,
        "reason": reason,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _checkpoint_codex_preflight(
    git: GitClient,
    integration_branch: str,
    write_log: Callable[[str], None],
    *,
    allowed_dirty: frozenset[str] = frozenset(),
    accept_baseline: bool = False,
    checkpoint_tracker: bool = True,
) -> frozenset[str]:
    """Checkpoint tracker exports and classify remaining dirty paths.

    Beads can update and stage its JSONL exports while Grind reads queue state.
    At startup, source changes are returned as handoff context instead of
    blocking Codex. Later calls accept the active candidate context.
    """
    if not git.is_git_repo():
        return frozenset()
    dirty = git.dirty_paths()
    if dirty is None:
        write_log("preflight: HALT — git status failed during ownership check")
        output.error(
            "grind: could not classify worktree ownership",
            hint="run git status, resolve the error, then re-run grind",
        )
        raise typer.Exit(code=1)
    if not dirty:
        return frozenset()

    unexpected = dirty - _TRACKER_EXPORT_PATHS - allowed_dirty
    if unexpected and not accept_baseline:
        rendered = ", ".join(sorted(unexpected))
        write_log(f"preflight: HALT — paths outside transaction ownership: {rendered}")
        output.error(
            "grind: worktree changed outside the recorded Codex transaction",
            hint=f"inspect these paths before resuming: {rendered}",
        )
        raise typer.Exit(code=1)

    tracker_paths = dirty & _TRACKER_EXPORT_PATHS if checkpoint_tracker else frozenset()
    if tracker_paths:
        write_log(
            "preflight: tracker changes detected; creating housekeeping commit: "
            + ", ".join(sorted(tracker_paths))
        )
    if tracker_paths and not git.commit_paths(tracker_paths, "chore: sync beads state"):
        write_log("preflight: HALT — tracker housekeeping commit failed")
        output.error(
            "grind: failed to checkpoint generated Beads state",
            hint="inspect the staged .beads/ files and git configuration",
        )
        raise typer.Exit(code=1)
    if tracker_paths:
        write_log("preflight: tracker housekeeping commit completed")
        _enforce_branch_discipline(
            git,
            integration_branch,
            write_log,
            phase="post-housekeeping",
        )

    remaining = git.dirty_paths()
    if remaining is None:
        output.error("grind: could not re-read worktree after tracker checkpoint")
        raise typer.Exit(code=1)
    if accept_baseline and remaining:
        write_log(
            "preflight: preserving dirty worktree as inherited dirty paths: "
            + ", ".join(sorted(remaining))
        )
    return remaining


#: Commit-message rules stated where the writer writes. The first two
#: autonomous landings both had their messages rejected for breaking rules
#: the contract never stated, so every writer-facing contract carries this
#: same rule set; tests/test_grind_prompt_content.py pins the phrasing.
_MESSAGE_RULES = (
    "Commit-message rules (a message that breaks one is replaced by a weaker "
    "deterministic assembly; only an over-long subject is repaired in place): "
    "an imperative subject of at most 72 characters counted with the "
    "`<issue-id>: ` prefix — describe the change, do not restate the issue "
    "title, no trailing period, no `...` — then a body of at least two "
    "paragraphs of plain-text prose, under 8,000 characters in all, naming in "
    "backticks at least one function, class, or file that actually appears in "
    "your diff and none that does not (a diff with no nameable symbol still "
    "names one of its files), never an inventory of the files touched, and "
    "never narration of how the commit was produced (attempt counts, "
    "criterion results, step names, owned-path hashes)."
)

#: The implementation phase rules injected ahead of the worker's condition.
#: Module-level so the prompt-content tests can hold its message guidance to
#: the same rule set the finalization gate enforces.
_IMPLEMENTATION_INSTRUCTION = (
    "Follow the one-issue goal-prompt loop. Session-close that id per "
    "AGENTS.md. " + _MESSAGE_RULES + " Do not pick a second issue."
)


def _resolve_merge_gate(config: Any) -> tuple[bool, float]:
    """`.ortusrc` merge-gate flag and timeout; invalid timeout falls back."""

    enabled = bool(config.get("merge_gate", False))
    raw = config.get("merge_gate_timeout", DEFAULT_MERGE_GATE_TIMEOUT)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_MERGE_GATE_TIMEOUT)
    if timeout < 0:
        timeout = float(DEFAULT_MERGE_GATE_TIMEOUT)
    return enabled, timeout


def _announced_push(git: GitClient, branch: str) -> bool:
    """`git push origin <branch>`, announced on the console as it happens.

    A push is the one act in a grind run that changes the world outside the
    machine, so it must never hide inside a synchronization log line: the
    console names the ref, the remote, and the commit range about to leave
    *before* the attempt — a push that hangs then reads as an in-flight push
    rather than silence — and confirms after. The range comes from refs
    already on hand (origin/<branch> before the push, the local branch tip);
    no network read is ever spent making an announcement prettier. When the
    remote-tracking ref is unresolvable (a branch's first push) the
    announcement says "all history" rather than inventing a range, and a push
    moving nothing says "already up to date" rather than a zero-commit range.

    Failure adds no console line here: each call site's existing failure
    narrative owns that. Every site that pushes routes through this helper so
    future push sites inherit the visibility instead of re-forgetting it.
    """
    old = git.remote_tip(branch)
    new = git.branch_tip(branch) or git.head_oid()
    if not old:
        span = "all history"
    else:
        count = git.local_ahead_of_remote(branch)
        if old == new or count == 0:
            span = "already up to date"
        else:
            noun = "commit" if count == 1 else "commits"
            span = f"{old[:7]}..{new[:7]}, {count} {noun}"
    output.progress("grind", f"pushing {branch} → origin ({span})")
    pushed = git.push(branch)
    if pushed:
        output.progress("grind", f"pushed {branch} → origin")
    return pushed


def _enforce_branch_discipline(
    git: GitClient,
    integration_branch: str,
    write_log: Callable[[str], None],
    *,
    phase: str,
    allowed_branch: str = "",
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

    # The active transaction's issue branch is a sanctioned location, not a
    # stray: a crash between the branch commit and the fast-forward leaves the
    # tree exactly here with a unique commit, and the journal replay — not a
    # HALT — is how that work reaches the integration branch.
    if allowed_branch and git.current_branch() == allowed_branch:
        write_log(
            f"branch-guard [{phase}]: on issue branch {allowed_branch!r} "
            "owned by the active transaction; leaving it for the journal replay"
        )
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
        pushed = _announced_push(git, integration_branch)
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
            raise typer.Exit(code=1)
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

    The in_progress figure is the size of the id set, not a query of its
    own. Both asked the tracker the same question under the same label
    filter, and the answer to one is the answer to the other. Every bd
    invocation is a process start — the dominant cost of a grind iteration
    under test — so the duplicate is dropped rather than kept for symmetry.
    A tracker error still reads as zero here, because the id set answers a
    failed query with an empty set exactly as the count answered with 0.
    """
    in_progress_ids = bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS)
    return StateSnapshot.from_counts(
        closed=bd.count_by_status("closed"),
        in_progress=len(in_progress_ids),
        open=bd.count_by_status("open", exclude_labels=EXCLUDED_LABELS),
        in_progress_ids=in_progress_ids,
    )


def _exit_counts(bd: BdClient, loop_view: StateSnapshot) -> tuple[int, int]:
    """`(in_progress, open)` as the operator sees the queue, at session end.

    The loop's own counts drop human-labelled issues so an escalated one
    cannot make the orchestrator spin, and reporting that view to the
    operator turned a queue holding parked work into a summary reading
    `0 in_progress, 0 open`. The exit line answers a different question —
    what is left here — so it counts every issue, label or not. Loop control,
    orphan detection, and the log's own ended line keep the excluded view.

    A count bd cannot answer comes back as 0, which is the undercount this
    exists to fix; the loop's figure is a floor under each raw count, because
    an unlabelled issue is in both views and the raw one can only be larger.
    """

    return (
        max(bd.count_by_status("in_progress"), loop_view.in_progress),
        max(bd.count_by_status("open"), loop_view.open),
    )


def _rollover_exhausted_epics(
    bd: BdClient, write_log: Callable[[str], None]
) -> None:
    """Close every ready epic whose children are all closed, repeatedly,
    until a pass closes nothing (closing one epic can surface another).

    Runs BEFORE the iteration's `before` snapshot so these harness closes
    are never misattributed to the worker by the closed-count delta, and so
    work unblocked by the rollover is claimable in the SAME iteration.
    Failures are logged and skipped — a bd hiccup here degrades to the old
    behavior (loop exits "queue blocked"), never a crash.
    """
    for _ in range(50):  # cascade guard; real chains are milestone-deep
        try:
            ready = bd.list_ready(exclude_labels=EXCLUDED_LABELS)
        except Exception as exc:
            write_log(f"epic rollover: bd ready failed ({exc}); skipping pass")
            return
        closed_any = False
        for entry in ready:
            entry_type = str(
                entry.get("issue_type") or entry.get("type") or ""
            ).strip()
            epic_id = str(entry.get("id") or "").strip()
            if entry_type != "epic" or not epic_id:
                continue
            try:
                full = bd.show(epic_id)
            except Exception as exc:
                write_log(f"epic rollover: bd show {epic_id} failed ({exc})")
                continue
            try:
                kids = bd.children(epic_id)
            except Exception as exc:
                write_log(
                    f"epic rollover: bd children {epic_id} failed ({exc})"
                )
                continue
            if not epic_is_exhausted(full, children=kids):
                continue
            try:
                bd.close(
                    epic_id,
                    reason="milestone rollover: all child issues closed",
                )
            except Exception as exc:
                write_log(f"epic rollover: close of {epic_id} failed ({exc})")
                continue
            write_log(f"epic rollover: closed exhausted epic {epic_id}")
            closed_any = True
        if not closed_any:
            return


def _legacy_prompt(custom_condition: str, backend: str = "claude") -> str:
    """The per-subprocess /goal prompt for the legacy `--condition` path.

    When the operator pins a custom condition we leave SELECTION to the worker
    (verbatim, every iteration) for backwards compatibility. The default path
    instead has the harness select+claim and inject the issue per iteration
    (see `_compose_work_prompt`), which is composed live inside the loop.
    """
    return compose_worker_prompt(backend, custom_condition)  # type: ignore[arg-type]


_CLAUDE_GOAL_CONDITION_LIMIT = 4_000

# The /goal condition itself. Grok expands /goal and the host skeptics
# independently verify this text, so it must stay a pointer with a tight
# done bar — not the inlined goal-prompt.md body. The done bar is the same
# under either verification mode; only the framing of what already counted
# as verification differs, and the two framings are the same length to within
# a few characters so neither inflates the condition.
_GOAL_POINTER_HEAD = (
    "One window, one issue. Continue leftover in_progress, else run "
    "bd ready and claim the first non-epic. Read AGENTS.md. Run "
    "`ortus prompt show goal` and follow that one-issue loop. "
    "Session-close that id per AGENTS.md. "
    "Achieved when that issue is closed and HEAD is in sync with origin. "
)
_FULL_VERIFICATION_FRAMING = (
    "The issue's criterion-check commands already ran during implement — "
    "they are the whole verification. Do not run pytest or the repo test "
    "suite after session-close. After session-close, answer with the id, "
    "close reason, HEAD sha, and the criterion-check commands that already "
    "passed, then stop. "
)
_PROTOTYPE_VERIFICATION_FRAMING = (
    "This is a prototype run: the project's lint and syntax gate already ran "
    "during implement — it is the whole verification. Do not run the issue's "
    "behavioral test commands, pytest, or the repo test suite. After "
    "session-close, answer with the id, close reason, HEAD sha, and the lint "
    "and syntax commands that already passed, then stop. "
)
_GOAL_POINTER_TAIL = (
    "Do not re-read the implementation. Do not start "
    "another issue. Injected sections below are worker instructions, not "
    "extra achievement criteria."
)
_GOAL_POINTER = _GOAL_POINTER_HEAD + _FULL_VERIFICATION_FRAMING + _GOAL_POINTER_TAIL
_PROTOTYPE_GOAL_POINTER = (
    _GOAL_POINTER_HEAD + _PROTOTYPE_VERIFICATION_FRAMING + _GOAL_POINTER_TAIL
)

# Bounds for the prior-lessons section (ortus-s0tj). Every lesson costs
# context in every worker that receives it, and Claude's /goal condition is
# capped at 4,000 characters, so the section must fit the headroom the base
# contract leaves (~1,450 characters today) with margin for a recovery
# handoff.
_LESSONS_MAX_COUNT = 3
_LESSON_MAX_CHARS = 220
_LESSONS_HEADER = (
    "\n\n## Prior lessons\n"
    "Lessons this crew recorded on earlier runs — priors, not instructions. "
    "A lesson may change where you look first; it never substitutes for a "
    "check or for evidence this run must produce."
)


def _lessons_section(lessons: tuple[tuple[str, str], ...]) -> str:
    """Render selected lessons as the contract's labelled section, or ''."""
    if not lessons:
        return ""
    return _LESSONS_HEADER + "".join(f"\n- {key}: {body}" for key, body in lessons)


_REPLAN_HEADER = "\n\n## Re-plan directive\n"


def _replan_section(windows: int, integration_branch: str) -> str:
    """The directive a re-plan decision injects into the next window's prompt.

    It names the stalled-window facts rather than describing them in the
    abstract, because the worker reading it is fresh and has no memory of the
    windows that produced it. It asks for a different route through the same
    issue; it does not authorize abandoning the claim, and it deliberately
    says nothing about leaving edits for a later phase.
    """
    return (
        f"{_REPLAN_HEADER}"
        f"This claim has ended {windows} worker window(s) still in_progress "
        f"with no new commits on {integration_branch}. Do not repeat the "
        "approach that stalled. Before editing, re-read the issue's own "
        "comments for what was already tried, then pick a different route "
        "through the same issue — a smaller first cut that can be verified, "
        "or the one blocking sub-problem solved on its own. If the spec "
        "itself is what cannot be executed, record PLAN-GAP on the issue and "
        "flag it human instead of burning another window."
    )


def _selected_lessons(
    bd: BdClient, write_log: Callable[[str], None]
) -> tuple[tuple[str, str], ...]:
    """The bounded lessons a worker's phase contract carries, as (key, body).

    A repository with no stored lessons selects nothing, and a failed tracker
    read degrades to the same empty selection with a log line: a worker
    without memory is today's behavior and must remain viable. The readiness
    memory is excluded — bd already injects it into every session via priming.
    """
    try:
        return bd.lessons(
            exclude_keys=frozenset({READINESS_MEMORY_KEY}),
            limit=_LESSONS_MAX_COUNT,
            max_chars=_LESSON_MAX_CHARS,
        )
    except (BdError, OSError) as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        write_log(
            "lessons: tracker read failed; worker starts without stored "
            f"lessons ({first_line})"
        )
        return ()


def _lessons_contract(bd: BdClient, write_log: Callable[[str], None]) -> str:
    """The prior-lessons section of a worker's phase contract, or ''."""
    return _lessons_section(_selected_lessons(bd, write_log))


def _log_dropped_lessons(
    lessons: tuple[tuple[str, str], ...],
    lessons_text: str,
    prompt: str,
    write_log: Callable[[str], None],
) -> None:
    """Name the lessons a composed prompt left out.

    Composition stays pure: :func:`_compose_work_prompt` drops the lessons
    section when it would push the Claude ``/goal`` condition past the cap.
    The caller detects that by looking for the section in the composed
    prompt and logs the omitted keys, so an owner decision that never reached
    a worker is visible in the run log instead of silently absent.
    """
    if not lessons_text or lessons_text in prompt:
        return
    keys = ", ".join(key for key, _ in lessons)
    write_log(
        "lessons: dropped from the worker prompt — the section would push the "
        f"Claude /goal condition past {_CLAUDE_GOAL_CONDITION_LIMIT} characters; "
        f"omitted keys: {keys}"
    )


def _resolve_verification(config: Any, prototype: bool) -> tuple[str, str]:
    """The run's verification mode, and the mode with its provenance for the log.

    The flag wins, then `.ortusrc`, then the default. The note names the
    source whenever it is not the default, and names an overridden `.ortusrc`
    pin outright, so a log reader knows a prototype run held a lighter bar
    and why it did.
    """
    configured = str(config.get("verification", DEFAULT_VERIFICATION_MODE))
    pinned = any(
        "verification" in layer.data
        for layer in getattr(config, "layers", ())
        if layer.source != "defaults"
    )
    if prototype:
        note = f"{VERIFICATION_PROTOTYPE} from --prototype"
        if pinned and configured != VERIFICATION_PROTOTYPE:
            note += f", .ortusrc pins {configured}"
        return VERIFICATION_PROTOTYPE, note
    if pinned:
        return configured, f"{configured} from .ortusrc"
    return configured, configured


def _resolve_prototype_gates(
    repo: Path, project_type: str, linter: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The (lint commands, syntax/compile commands) a prototype run verifies with.

    A typed project resolves one command per kind from the tables beside
    init's linter defaults. `polyglot` — or a type the tables do not know —
    resolves the syntax gate of every language whose marker file the
    repository root carries. A `linter` of none drops the lint command
    rather than inventing one; the worker's instruction then says so.
    """
    lint = PROTOTYPE_LINT_COMMANDS.get(linter)
    lints = (lint,) if lint else ()
    syntax = PROTOTYPE_SYNTAX_COMMANDS.get(project_type)
    if syntax is not None:
        return lints, (syntax,)
    detected = tuple(
        PROTOTYPE_SYNTAX_COMMANDS[language]
        for language, markers in LANGUAGE_MARKERS.items()
        if any((repo / marker).is_file() for marker in markers)
    )
    return lints, detected


def _gate_summary(lints: tuple[str, ...], syntaxes: tuple[str, ...]) -> str:
    """One log-line description of a prototype run's gate."""
    parts = []
    if lints:
        parts.append("lint: " + ", ".join(lints))
    if syntaxes:
        parts.append("syntax: " + ", ".join(syntaxes))
    if not parts:
        return "no linter or compile gate resolved; syntax-parse only"
    return "; ".join(parts)


def _prototype_verification_section(
    lints: tuple[str, ...], syntaxes: tuple[str, ...]
) -> str:
    """The prototype verification section of a worker's contract.

    Injected after the CodeGraph phase contract, and never dropped for the
    Claude cap the way lessons are: it is the run's bar, not a prior. It
    names the commands so the worker runs the resolved gate rather than
    choosing one, and states what it must not run. A project with no gate
    at all is told verification is syntax-parse only — never left with a
    silent no-op.
    """
    commands = [f"`{command}`" for command in (*lints, *syntaxes)]
    if len(commands) > 2:
        named = ", ".join(commands[:-1]) + ", and " + commands[-1]
    else:
        named = " and ".join(commands)
    if named:
        gate = (
            f"Lint and syntax are the whole verification: run {named}, fix "
            "what they report, and session-close on a clean pass."
        )
    else:
        gate = (
            "No linter or compile gate resolved for this project, so "
            "verification is syntax-parse only: parse every file you changed "
            "with its language's own parser and session-close on a clean parse."
        )
    return (
        "\n\n## Prototype verification\n"
        f"This run holds the prototype bar. {gate} Do not run the issue's "
        "behavioral test commands or the repo test suite; its Criterion checks "
        "stay in the work spec as the record, not as this run's bar."
    )


def _compose_work_prompt(
    template: str,
    issue: dict,
    backend: str = "claude",
    *,
    phase_instruction: str = "",
    phase_contract_text: str = "",
    verification_text: str = "",
    lessons_text: str = "",
    bound_issue_id: str | None = None,
    goal_template: str | None = None,
    semantic_advice: str = "",
    stable_prefix: bool = False,
    replan_text: str = "",
) -> str:
    """Build one backend-appropriate prompt for a single goal-prompt iteration.

    The /goal condition is ``_GOAL_POINTER`` (worker fetches the loop body
    via ``ortus prompt show goal``). Ordinary calls do not inject a claimed id.
    ``template`` and ``issue`` remain for compatibility with existing callers.
    Only an explicit ``bound_issue_id`` uses the issue, with a validated
    ``goal_template`` that supports binding. Ungated callers ignore both.

    ``verification_text`` is the prototype verification section. When it is
    present the pointer carries the prototype framing — lint and syntax are
    the whole verification — in place of the criterion-check one, and the
    section follows the phase contract. Empty text composes today's full-mode
    condition byte for byte.

    ``lessons_text`` is the one optional section: when appending it would
    push the Claude ``/goal`` condition past the cap it is dropped rather
    than halting the run. The 4,000-character cap is Claude-only.

    ``stable_prefix`` selects the cache-friendly ordering. Off, the sections
    compose in today's order byte for byte. On, the two segments that carry
    the claimed bead — the readiness advice and the Bound issue contract —
    move behind every segment that is identical across the beads of a run,
    so a provider prefix cache sees the same leading bytes each iteration
    instead of losing them to a bead id sitting in the middle.

    ``replan_text`` is the re-plan directive a stuck decision injects. Like
    the bound contract it is never droppable — it is the whole reason this
    window differs from the one that stalled — so its length comes out of the
    cap before the optional sections are measured, and it composes behind
    them in both orderings because it describes this window, not the run.
    """
    del template

    task = _PROTOTYPE_GOAL_POINTER if verification_text else _GOAL_POINTER
    bound_text = ""
    if bound_issue_id is not None:
        from ortus.core.judge_claim import bound_issue_section, validate_bound_goal

        validate_bound_goal(goal_template or "")
        task = task.replace(
            "Continue leftover in_progress, else run bd ready and claim the first non-epic.",
            "Continue only the injected bound issue; do not select or claim another id.",
            1,
        )
        bound_text = bound_issue_section(issue, bound_issue_id)
        if not stable_prefix:
            task += bound_text
            bound_text = ""
    if phase_instruction:
        task = phase_instruction.rstrip() + "\n\n" + task
    task += phase_contract_text + verification_text
    wrap_limit = _CLAUDE_GOAL_CONDITION_LIMIT if backend == "claude" else None
    # A bound contract names the only id the worker may touch, so it is never
    # droppable: its length comes out of the cap before the optional sections
    # are measured against what remains.
    headroom = (
        wrap_limit - len(bound_text) - len(replan_text)
        if wrap_limit is not None else None
    )
    optional = (
        (lessons_text, semantic_advice)
        if stable_prefix
        else (semantic_advice, lessons_text)
    )
    for section in optional:
        if section and (headroom is None or len(task) + len(section) <= headroom):
            task += section
    task += replan_text
    task += bound_text
    if wrap_limit is not None and len(task) > wrap_limit:
        raise BackendError(
            "internal Claude /goal condition exceeds the 4,000-character limit "
            f"({len(task)} characters)"
        )
    return compose_worker_prompt(backend, task)  # type: ignore[arg-type]


def _stale_completion_contract_diagnostic(
    composed: str,
    *,
    repo: Path,
    home: Path | None = None,
    audited: bool = False,
) -> str | None:
    """If the worker would be told to leave the issue open, name the source.

    Inspects the composed grind prompt (phase instruction plus CodeGraph
    contract) and the resolved goal prompt the worker fetches via
    ``ortus prompt show goal`` — the variant this run serves, so an audited
    run is judged on the text its worker will actually read. A repo or user override that still carries
    the retired leave-open sentence is reported with its path so the
    operator can rewrite or remove it; the override is never overwritten
    in place. Stock text that still carries the sentence is reported as an
    internal contract defect.
    """
    hits: list[str] = []
    needle = LEAVE_OPEN_FOR_VERIFICATION.lower()
    if needle in composed.lower():
        hits.append("the composed CodeGraph implementation phase contract")
    try:
        goal = resolve_named_prompt("goal", repo=repo, home=home, audited=audited)
    except Exception:
        goal = None
    if goal is not None and needle in goal.text.lower():
        if goal.source == "bundled":
            hits.append("the bundled goal prompt")
        elif goal.path is not None:
            hits.append(f"{goal.source} override {goal.path}")
        else:
            hits.append(f"{goal.source} override")
    if not hits:
        return None
    source = " and ".join(hits)
    return (
        f"grind: stale completion contract in {source}: the worker is told to "
        "leave candidate edits for an unscheduled verification phase. The "
        "one-issue loop requires session-close. Do not overwrite a custom "
        "override in place; rewrite or remove it (for a stamped eject, "
        "`ortus prompt eject goal --force`), then re-run."
    )


def _done_bar_met(
    bd: BdClient,
    git: GitClient,
    baseline_closed: int,
    integration_branch: str,
    bound_issue_id: str | None = None,
) -> str | None:
    """Label when closed-count grew, HEAD is in sync, and the tree is clean.

    Predicted id does not matter: a worker that claimed a different ready
    issue still trips the bar. Missing origin tracking is not in sync.
    A dirty worktree is not done: the worker still has to commit and push.
    ``dirty_paths`` returning None is not an empty tree — same as a tracker
    error, a poll must not kill a live worker.
    """

    try:
        if not git.remote_tip(integration_branch):
            return None
        if git.local_ahead_of_remote(integration_branch) != 0:
            return None
        dirty = git.dirty_paths()
        if dirty != frozenset():
            return None
        if bound_issue_id is not None:
            return (
                f"closed {bound_issue_id}"
                if bd.show(bound_issue_id).get("status") == "closed"
                else None
            )
        closed = bd.count_by_status("closed")
    except Exception:
        return None
    if closed > baseline_closed:
        return f"closed {baseline_closed}->{closed}"
    return None


def _flagged_claims(bd: BdClient) -> set[str]:
    """In-progress ids that carry an excluded label, each confirmed by name.

    The difference between every in_progress id and the ids that survive
    ``EXCLUDED_LABELS`` nominates candidates. Each is then confirmed against
    its own labels, because ``in_progress_ids`` answers a failed query with an
    empty set, and a failure on the excluding query alone would otherwise make
    every live claim look flagged. A tracker error on the confirmation
    propagates, so the caller treats the poll as unanswered.
    """

    every = bd.in_progress_ids()
    if not every:
        return set()
    unflagged = bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS)
    flagged: set[str] = set()
    for issue_id in every - unflagged:
        labels = bd.show(issue_id).get("labels") or []
        if any(label in EXCLUDED_LABELS for label in labels):
            flagged.add(issue_id)
    return flagged


_FLAGGED_REASON = "claim flagged human"


def _reap_reason(
    bd: BdClient,
    git: GitClient,
    *,
    baseline_closed: int | None,
    flagged_at_start: frozenset[str] | None,
    integration_branch: str,
    bound_issue_id: str | None = None,
) -> str | None:
    """Why the running worker should be reaped now, or None to let it run.

    Two facts end a window from the harness side, and both describe a worker
    with nothing left to do. The done bar: it closed and pushed, so whatever
    it is still saying is the /goal Stop hook holding it. A flagged claim: it
    took the PLAN-GAP exit — commented, labelled its issue human, and tried
    to stop — and the same hook holds it, because the condition it answers to
    reads "closed and in sync" and a flagged issue is neither. Only a claim
    that was unflagged when the window began counts; a leftover flagged claim
    was excluded at startup and is not this worker's. A check whose baseline
    could not be read is skipped, and a tracker or git error during the poll
    is an unanswered poll, never a reap.
    """

    if baseline_closed is not None or bound_issue_id is not None:
        label = _done_bar_met(
            bd, git, baseline_closed or 0, integration_branch, bound_issue_id,
        )
        if label:
            return f"done bar met ({label}, in sync)"
    if bound_issue_id is not None:
        try:
            issue = bd.show(bound_issue_id)
            if "human" in (issue.get("labels") or []):
                return f"{_FLAGGED_REASON} ({bound_issue_id})"
        except Exception:
            pass
        return None
    if flagged_at_start is None:
        return None
    try:
        flagged = _flagged_claims(bd) - flagged_at_start
    except Exception:
        return None
    if flagged:
        return f"{_FLAGGED_REASON} ({', '.join(sorted(flagged))})"
    return None


def _hand_back_comment(window: int) -> str:
    return (
        f"grind: handed back after window {window}. The worker claimed this "
        "issue from bd ready although it was open and labelled human when the "
        "window began, so it could never finish it. The claim is reverted to "
        "open, the label is kept, and the packet is untouched. An issue "
        "labelled human is the operator's: repair or unlabel it before a "
        "worker can take it."
    )


def _hand_back_misclaims(
    bd: BdClient,
    *,
    flagged_at_start: frozenset[str],
    human_open_at_start: frozenset[str] | None,
    window: int,
    write_log: Callable[[str], None],
) -> set[str]:
    """Revert the reaped worker's claims on issues that were the operator's.

    A worker that runs `bd ready` and claims the first non-epic can land on
    an issue that already carried the human label: the listing does not
    exclude it, and only the goal prompt's rule keeps the worker off it. The
    flagged-claim reap then fires within a poll, but the claim used to stay
    in_progress, and the next window's worker read it as a leftover to
    continue — every later window inherited an issue no worker can finish,
    until someone unclaimed it by hand. Handing it back here is what lets the
    next window start clean.

    Only a claim on an issue that was open and human-labelled when the window
    began qualifies. A worker that flags its own claim on the PLAN-GAP exit
    left a comment for the operator and keeps that claim in_progress, and a
    claim flagged before the window was excluded at startup and is not this
    worker's. The revert is status only: the label stays and no packet field
    is touched, so the readiness hash is unchanged. A tracker error on any
    step is logged and that claim is left as it is; the loop never crashes on
    a hand-back. Returns the ids handed back.
    """

    if not human_open_at_start:
        return set()
    try:
        flagged = _flagged_claims(bd) - flagged_at_start
    except Exception as exc:
        write_log(
            f"iter {window}: could not read the flagged claims to hand back ({exc})"
        )
        return set()
    handed_back: set[str] = set()
    for issue_id in sorted(flagged & human_open_at_start):
        try:
            bd.update_status(issue_id, ISSUE_OPEN)
            bd.add_comment(issue_id, _hand_back_comment(window))
        except Exception as exc:
            write_log(f"iter {window}: could not hand back {issue_id} ({exc})")
            continue
        handed_back.add(issue_id)
        write_log(
            f"iter {window}: handed back {issue_id} (open and labelled human "
            "at window start; claim reverted to open, label kept)"
        )
    return handed_back


def _parked_head_comment(window: int, blocked: list[str]) -> str:
    labels = ", ".join(sorted(blocked))
    return (
        f"grind: claim reverted after window {window}. This issue ended its "
        f"window carrying the excluded label(s) {labels}, so no worker may "
        "run for it: every snapshot gate ignores an excluded claim, and a "
        "finished candidate would be silently dropped. The claim is reverted "
        "to open, the label is kept, and the packet is untouched — read the "
        "newest comment, decide, and relabel it for the queue."
    )


def _release_parked_head(
    bd: BdClient,
    issue_id: str,
    blocked: list[str],
    *,
    window: int,
    write_log: Callable[[str], None],
) -> bool:
    """Revert a head that ends its window excluded-labelled back to open.

    The queue continues past an excluded claim, so leaving it in_progress
    stranded it: no later window would resume it, no worker could finish it,
    and the operator only saw it by reading bd directly. The status goes back
    to open the way `_hand_back_misclaims` returns a mis-claim — the label
    stays, no packet field is touched, and the readiness hash is unchanged —
    so the issue sits in the operator's queue as what it is, unclaimed work
    awaiting a decision.

    The caller's own in_progress judgment is the guard: this runs on the
    post-mortem state of a window whose worker is already dead, so there is
    no live assignee to race. A tracker error is logged and the claim is left
    as it is; the loop never crashes on a release. Returns whether the claim
    was reverted.
    """

    try:
        bd.update_status(issue_id, ISSUE_OPEN)
        bd.add_comment(issue_id, _parked_head_comment(window, blocked))
    except Exception as exc:  # noqa: BLE001 - a tracker hiccup never ends the loop
        write_log(f"iter {window}: could not release {issue_id} ({exc})")
        return False
    write_log(
        f"iter {window}: released {issue_id} (claim reverted to open, "
        "label kept) so it is not left in_progress with no worker able to "
        "take it"
    )
    return True


def _claude_goal_rejection(log_path: Path, *, start_offset: int) -> str | None:
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


def _unready_skip_line(title: str, report: ReadinessReport) -> str:
    """Console-altitude skip line: title first, id in parentheses, one clause.

    The full section-by-section enumeration keeps serving the log; the
    console gets the summary a colleague would say aloud. The title is passed
    as plain text: `output.warn` and `output.error` escape it themselves, so a
    bracketed title prints literally rather than being read as markup, and
    escaping it here as well would leave a stray backslash on the line.
    """

    label = title if title.strip() else report.issue_id
    return f'skipped "{label}" ({report.issue_id}) — {report.summary()}'


def _flag_unready_for_human(
    bd: BdClient,
    reports: list[ReadinessReport],
    write_log: Callable[[str], None],
) -> None:
    """Label each unready leaf human and comment the readiness diagnostic.

    Runs at skip time, the moment the readiness guard rejects a leaf —
    even when a sibling passes and gets selected — so the label (not the
    goal prompt alone) keeps the worker's own `bd ready` from claiming a
    spec-free issue. A failed label add still warns and continues so every
    remaining id is attempted; grind never falls back to a repair worker
    from this path.
    """

    for report in reports:
        diagnostic = report.diagnostic()
        try:
            bd.add_label(report.issue_id, "human")
        except Exception as exc:
            write_log(
                f"readiness: could not label {report.issue_id} human ({exc}); "
                "the worker may still claim it this iteration"
            )
            output.warn(
                f"could not label {report.issue_id} human ({exc}); "
                "it stays claimable this iteration"
            )
        try:
            bd.add_comment(
                report.issue_id,
                "readiness schema v1 failed; grind will not repair this "
                "packet. The human label keeps this issue out of grind's "
                "queue; after repairing the spec, run: bd label remove "
                f"{report.issue_id} human.\n\n{diagnostic}",
            )
        except Exception as exc:
            write_log(
                f"readiness: could not comment on {report.issue_id} ({exc})"
            )
        write_log(f"readiness: flagged {report.issue_id} human")


#: Marker-comment prefix that persists a claim's consecutive no-close window
#: count in bd. Grind sessions are separate processes, so bd comments are the
#: only cross-process store; the format must stay stable across versions
#: because old markers are re-read by newer grinds.
_NO_CLOSE_MARKER_PREFIX = "ortus-grind: no-close window "

#: Marker-comment prefix for the re-plan directives a claim has already been
#: given. Same cross-process store and same lenient parse as the no-close
#: counter, kept separate so the older marker's format is untouched.
_REPLAN_MARKER_PREFIX = "ortus-grind: replan window "

#: The fail-open baseline's threshold, re-exported for the log lines and
#: tests that name it. The rule itself now lives beside the decision it is
#: the fallback for.
_WEDGED_WINDOW_THRESHOLD = WEDGED_WINDOW_THRESHOLD


def _marker_count(bd: BdClient, issue_id: str, prefix: str) -> int:
    """The newest count a `<prefix><n>` marker comment records on the issue.

    Parsed leniently: no marker, an unreadable comment list, or an
    unparseable count (an older version's marker with the same prefix) all
    read as zero — a fresh budget, never a spurious escalation.
    """
    try:
        existing = bd.comments(issue_id)
    except Exception:
        return 0
    count = 0
    for comment in existing:
        if not isinstance(comment, dict):
            continue
        for key in ("body", "text", "comment", "content"):
            body = str(comment.get(key) or "")
            marker = body.find(prefix)
            if marker == -1:
                continue
            tail = body[marker + len(prefix) :].split()
            try:
                count = int(tail[0]) if tail else 0
            except ValueError:
                count = 0
            break
    return count


def _no_close_window_count(bd: BdClient, issue_id: str) -> int:
    """The claim's recorded consecutive no-close window count."""
    return _marker_count(bd, issue_id, _NO_CLOSE_MARKER_PREFIX)


def _replan_window_count(bd: BdClient, issue_id: str) -> int:
    """How many re-plan directives this claim has already been given.

    A directive that did not work the first time is weak evidence for a
    second one, so the stuck decision reads this and shifts replan mass
    toward escalate — a slope, never a floor.
    """
    return _marker_count(bd, issue_id, _REPLAN_MARKER_PREFIX)


def _record_no_close_window(
    bd: BdClient, issue_id: str, count: int, write_log: Callable[[str], None]
) -> None:
    """Persist the claim's consecutive no-close window count as a marker comment.

    A failed write warns and moves on — a bd hiccup must not strand a healthy
    claim; the next window simply re-reads the previous count.
    """
    try:
        bd.add_comment(issue_id, f"{_NO_CLOSE_MARKER_PREFIX}{count}")
    except Exception as exc:
        write_log(
            f"wedged-claim counter: could not record no-close window {count} "
            f"on {issue_id} ({exc}); the previous count stands"
        )


def _record_replan_window(
    bd: BdClient, issue_id: str, count: int, write_log: Callable[[str], None]
) -> None:
    """Persist how many re-plan directives this claim has been given.

    A failed write warns and moves on: the decision that follows simply reads
    a lower count and is that much readier to try another directive, which is
    the safe direction to be wrong in.
    """
    try:
        bd.add_comment(issue_id, f"{_REPLAN_MARKER_PREFIX}{count}")
    except Exception as exc:
        write_log(
            f"stuck decision: could not record replan window {count} on "
            f"{issue_id} ({exc}); the previous count stands"
        )


def _claim_excluded_labels(bd: BdClient, issue_id: str) -> list[str]:
    """Excluded labels currently carried by a claim.

    A resume names its issue directly and so bypasses the label filter every
    snapshot gate applies; the resume decision has to ask instead. An issue
    that cannot be read reads as unlabeled — the claim then resumes and the
    normal gates judge it, rather than being parked on a bd hiccup.
    """
    try:
        issue = bd.show(issue_id)
    except Exception:
        return []
    labels = {str(label) for label in (issue.get("labels") or ())}
    return sorted(labels & set(EXCLUDED_LABELS))


def _escalate_wedged_claim(
    bd: BdClient,
    issue_id: str,
    windows: int,
    integration_branch: str,
    write_log: Callable[[str], None],
    *,
    log_context: str,
) -> None:
    """Hand a wedged claim to the human queue instead of resuming it again.

    The label (the same convention the readiness gate uses) keeps the issue
    out of every snapshot; the claim itself stays in_progress so the
    worker-tree context is preserved for the operator. Either bd write may
    fail without stopping the run: an unlabeled claim simply stays resumable
    and burns the same counter again.
    """
    escalation = (
        f"wedged claim: {issue_id} burned {windows} consecutive worker "
        "windows that ended still in_progress with no new commits on "
        f"{integration_branch}; escalating to the human queue instead of "
        "resuming"
    )
    write_log(f"{log_context}: {escalation}")
    output.warn(escalation)
    try:
        bd.add_label(issue_id, "human")
    except Exception as exc:
        write_log(
            f"wedged claim: could not label {issue_id} "
            f"human ({exc}); it stays resumable next window"
        )
        output.warn(
            f"could not label {issue_id} human ({exc}); "
            "it stays resumable next window"
        )
    try:
        bd.add_comment(
            issue_id,
            f"wedged claim escalated: {windows} consecutive worker windows "
            "ended with this claim still in_progress and no new commits on "
            f"{integration_branch}. The human label keeps this issue out of "
            "grind's queue; after repairing the spec, run: bd label remove "
            f"{issue_id} human.",
        )
    except Exception as exc:
        write_log(f"wedged claim: could not comment on {issue_id} ({exc})")


def _decide_stuck(
    bd: BdClient,
    repo: Path,
    config: JudgeConfig,
    run_id: UUID,
    issue_id: str,
    verdict: OutcomeVerdict | None,
    windows: int,
    branch_advanced: bool,
    write_log: Callable[[str], None],
) -> tuple[StuckDecision, StuckAction]:
    """Read what to do with a claim whose window ended without a close.

    Returns the decision and the action actually applied. Shadow mode reads
    the vector and applies the fail-open baseline, so a seat can watch the
    decision it would have taken for a whole run before letting it move a
    claim. Without the post-turn phase there is no verdict to read and no
    decision log to write: the baseline runs and nothing new is recorded, so
    a repository with Jev off keeps the run it had.

    A decision log that cannot be written is not a reason to strand a claim.
    The action stands and the failure goes to the run log, where the operator
    is already reading.
    """
    decision = decide_stuck_claim(
        verdict, windows, branch_advanced,
        replans=_replan_window_count(bd, issue_id),
    )
    applied = decision.action
    if config.mode == JudgeMode.SHADOW and not decision.fail_open:
        applied = baseline_stuck_action(windows)
    if config.post_turn:
        try:
            log_stuck_decision(repo, config, run_id, issue_id, decision, applied)
        except Exception as exc:
            write_log(
                f"stuck decision: could not log the decision for {issue_id} "
                f"({exc}); the action still stands"
            )
    vector = ", ".join(
        f"{name}={value:.3f}" for name, value in decision.vector().items()
    )
    write_log(
        f"stuck decision for {issue_id}: {applied.value} (windows={windows}, "
        f"advanced={branch_advanced}, fail_open={decision.fail_open}, {vector})"
    )
    return decision, applied


def _announce_wedged_escalation(escalated: tuple[str, int]) -> None:
    """Session-end hint naming the escalation instead of 'run grind again'."""
    issue_id, windows = escalated
    output.progress(
        "grind",
        f"escalated {issue_id} to the human queue after {windows} no-close "
        f"windows; repair the spec, then: bd label remove {issue_id} human",
    )


def _log_writer(log_path: Path) -> Callable[[str], None]:
    """Tee-style logger: write a timestamped line to log_path; terminal stays quiet."""

    def _write(msg: str) -> None:
        line = f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)

    return _write


def _discard_leftover_journal(
    repo: Path, write_log: Callable[[str], None]
) -> None:
    """Ignore a leftover candidate journal. It is not a resume key.

    Leftover work is the leftover ``in_progress`` claim in bd plus the git
    tree. A leftover ``finalized-*`` file must not HALT the run.
    """

    path = repo / "logs" / "grind-transaction.json"
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError as exc:
        write_log(
            "startup: leftover candidate journal could not be removed "
            f"({exc}); ignoring it"
        )
        return
    write_log(
        "startup: discarded leftover candidate journal; leftover work is "
        "in_progress in bd plus the git tree"
    )


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
        help=(
            "Legacy: custom per-iteration /goal condition whose worker also "
            "selects its own issue (replaces the grind-claimed work-issue.txt "
            "flow and its verified-close transaction)."
        ),
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
        # 1800 predated the candidate transaction, when a worker implemented and
        # stopped. A worker now runs the work spec's targeted suite during
        # implementation and a fresh verifier runs it again, and bd costs about
        # a second per invocation, so the changed-surface suites here take 10-15
        # minutes each. Real workers were killed mid-verification at 30 minutes
        # holding finished work (ortus-6ur4), which strands a candidate rather
        # than bounding a hang. 5400 still bounds a genuine hang.
        5400,
        "--worker-timeout",
        help=(
            "Hard cap (secs) on a single iteration's worker subprocess. On exceed, "
            "SIGTERM then SIGKILL the worker's whole process group (killing any child "
            "bd/dolt/build processes and releasing their locks). Codex preserves the "
            "claimed owned paths for restart; Claude runs bd-state/orphan-policy "
            "recovery. 0 disables the watchdog (workers may then hang indefinitely)."
        ),
    ),
    integration_branch: Optional[str] = typer.Option(
        None,
        "--integration-branch",
        help=(
            "Branch grind pins the working tree to. A closed issue's commit must "
            "land on origin/<branch> to be deployable; grind re-asserts this branch "
            "each iteration and halts loudly if a worker strands work on a side "
            "branch instead of silently leaving origin stale. Defaults to "
            "integration_branch in .ortusrc, else 'main'."
        ),
    ),
    fast: bool = typer.Option(
        False, "--fast", help="Use claude --fast (premium output)."
    ),
    implement_model: Optional[str] = typer.Option(
        None, "--implement-model", help="Override the implementation profile model."
    ),
    implement_reasoning_effort: Optional[str] = typer.Option(
        None,
        "--implement-reasoning-effort",
        help="Override the implementation profile reasoning effort.",
    ),
    verify_model: Optional[str] = typer.Option(
        None, "--verify-model", help="Override the verification profile model."
    ),
    verify_reasoning_effort: Optional[str] = typer.Option(
        None,
        "--verify-reasoning-effort",
        help="Override the verification profile reasoning effort.",
    ),
    docker: bool = typer.Option(
        False, "--docker", help="Run claude inside docker sandbox instead of bwrap."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print resolved flags + composed per-iteration prompt; do not spawn claude.",
    ),
    judge: Optional[bool] = typer.Option(
        None, "--judge/--no-judge", help="Enable or disable the pre-turn judge gate."
    ),
    judge_seat: Optional[str] = typer.Option(
        None, "--judge-seat", help="Select an explicit configured judge seat alias."
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help=(
            "claude, codex, grok, local, or opencode; overrides ORTUS_BACKEND and .ortusrc "
            "('all' is an init provisioning option, not a run backend)."
        ),
    ),
    codegraph: Optional[CodeGraphMode] = typer.Option(
        None,
        "--codegraph",
        help="CodeGraph policy: off|auto|required (defaults from .ortusrc).",
        case_sensitive=False,
    ),
    prototype: bool = typer.Option(
        False,
        "--prototype",
        help=(
            "Prototype verification for this run: the worker proves each issue "
            "with the project's linter plus a syntax/compile gate and never runs "
            "its behavioral test commands or the repo suite. Overrides "
            "`verification` in .ortusrc, whose default is full."
        ),
    ),
) -> None:
    """Drive the bd queue via a subprocess-per-task /goal loop (ortus-3ico)."""
    target = resolve_repo(repo)
    try:
        resolved_backend = resolve_backend(backend, repo=target)
        config = load_config(repo=target)
        judge_config = parse_judge_config(config, judge=judge, judge_seat=judge_seat)
        enforce_judge = judge_config.enabled and judge_config.mode == JudgeMode.ENFORCE
        bind_worker = enforce_judge or judge_config.pre_tool
        if judge_config.pre_tool:
            check_pre_tool(target, resolved_backend, docker=docker)
        prompt_audit = audit_enabled(config)
        prompt_variant_note = audit_note(config)
        stable_prefix = stable_prefix_enabled(config)
        prefix_order_note = stable_prefix_note(config)
        goal_template = ""
        if bind_worker:
            goal_template = resolve_named_prompt(
                "goal", repo=target, audited=prompt_audit
            ).text
            validate_bound_goal(goal_template, condition)
        # Only the operator-served backends have a server to reach. The
        # table's rules run here so a missing [local] fails with one message
        # whether the backend came from the flag, the environment, or .ortusrc.
        local_config = (
            load_local_config(config)
            if resolved_backend in LOCAL_TABLE_BACKENDS
            else None
        )
        integration_branch = integration_branch or config.get(
            "integration_branch", DEFAULT_INTEGRATION_BRANCH
        )
        merge_gate, merge_gate_timeout = _resolve_merge_gate(config)
        verification_mode, verification_note = _resolve_verification(
            config, prototype
        )
        implement_profile = config.resolve_profile(
            resolved_backend,
            Phase.IMPLEMENT,
            model=implement_model,
            reasoning_effort=implement_reasoning_effort,
        )
        verify_profile = config.resolve_profile(
            resolved_backend,
            Phase.VERIFY,
            model=verify_model,
            reasoning_effort=verify_reasoning_effort,
        )
        finalize_profile = config.resolve_profile(
            resolved_backend, Phase.FINALIZE
        )
    except (BackendError, ProfileError) as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)

    # The prototype bar is resolved once per run from the project facts
    # `.ortusrc` records, and its section rides in every worker prompt. Full
    # mode composes no section, so the condition is unchanged.
    verification_text = ""
    prototype_gates: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    if verification_mode == VERIFICATION_PROTOTYPE:
        project_type = str(config.get("project_type") or "polyglot")
        linter = str(config.get("linter") or LINTER_DEFAULTS.get(project_type, "none"))
        prototype_gates = _resolve_prototype_gates(target, project_type, linter)
        verification_text = _prototype_verification_section(*prototype_gates)

    configured_mode = config.get("codegraph", "auto")
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
        codegraph_probe = codegraph_adapter.probe(
            target, codegraph_mode, backend=resolved_backend
        )
    except CodeGraphUnavailable as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
    if not dry_run:
        if codegraph_mode is CodeGraphMode.OFF:
            output.progress("grind", "CodeGraph disabled by policy")
        elif codegraph_probe.available:
            output.progress(
                "grind", "CodeGraph child registration ready; awaiting handshake"
            )
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
    work_template = (
        read_work_issue_condition(audited=prompt_audit) if harness_select else ""
    )

    if dry_run:
        output.info(
            f"judge:          {'enabled' if judge_config.enabled else 'disabled'} "
            f"mode={judge_config.mode.value} model={judge_config.model} "
            f"failure={judge_config.failure_mode.value} "
            f"seat={judge_config.seat} timeout={judge_config.timeout_seconds}s"
        )
        if judge_config.pre_tool:
            output.info("judge pre_tool: enabled (Claude run-scoped hook)")
        if judge_config.post_turn:
            output.info("judge post_turn: enabled (advisory outcome)")
        if judge_config.enabled:
            output.info(
                "judge routes:   " + ", ".join(route.value for route in judge_config.routes)
            )
            output.info(f"judge text:     {judge_config.include_issue_text}")
        output.info(f"repo:           {target}")
        output.info(f"tasks:          {tasks}")
        output.info(f"iterations:     {iterations}")
        output.info(f"orphan-policy:  {orphan_policy.value}")
        output.info(f"integration:    {integration_branch}")
        output.info(f"idle-sleep:     {idle_sleep}s")
        output.info(
            f"worker-timeout: {worker_timeout}s"
            if worker_timeout > 0
            else "worker-timeout: off"
        )
        output.info(f"fast:           {fast}")
        output.info(f"docker:         {docker}")
        output.info(f"backend:        {resolved_backend}")
        if local_config is not None:
            output.info(f"local:          {local_config.display}")
        output.info(f"implement:      {implement_profile.display_name}")
        output.info(f"verify:         {verify_profile.display_name}")
        output.info(f"finalize:       {finalize_profile.display_name}")
        output.info(f"codegraph:      {codegraph_mode.value}")
        output.info(f"verification:   {verification_note}")
        output.info(f"prompt text:    {prompt_variant_note}")
        output.info(f"prefix order:   {prefix_order_note}")
        output.info(
            "merge-gate:     "
            + (
                f"on, timeout {int(merge_gate_timeout)}s"
                if merge_gate
                else "off"
            )
        )
        output.info(
            "select:         " + (
                "judge-bound issue" if bind_worker else
                "worker (goal-prompt claim)" if harness_select else
                "worker (legacy --condition)"
            )
        )
        output.info("--- per-iteration prompt ---")
        if harness_select:
            dry_prompt = _compose_work_prompt(
                work_template,
                {"id": "<ISSUE_ID>", "title": "<ISSUE_DETAILS>",
                 "status": "in_progress", "assignee": "preview"},
                resolved_backend,
                phase_instruction=_IMPLEMENTATION_INSTRUCTION,
                phase_contract_text=phase_contract(
                    CodeGraphPhase.IMPLEMENTATION, codegraph_probe
                ),
                verification_text=verification_text,
                bound_issue_id="<ISSUE_ID>" if bind_worker else None,
                goal_template=goal_template if bind_worker else None,
                stable_prefix=stable_prefix,
            )
            conflict = _stale_completion_contract_diagnostic(
                dry_prompt, repo=target, audited=prompt_audit
            )
            if conflict:
                output.error(conflict)
                raise typer.Exit(code=1)
            output.info(
                dry_prompt
                + (
                    "\n(grind judges and binds one owned issue before worker launch.)"
                    if enforce_judge else
                    "\n(grind binds one owned issue before installing tool hooks.)"
                    if judge_config.pre_tool else
                    "\n(the worker orients, continues leftover in_progress or "
                    "runs bd ready, and claims; grind only decides whether to spawn.)"
                )
            )
        else:
            output.info(_legacy_prompt(condition, resolved_backend))
        return

    if not _make_git(target).is_git_repo():
        output.error("grind: working tree is not a git repository")
        raise typer.Exit(code=1)

    # Phase 0 — sandbox precondition (Tier 1 native vs Tier 2 docker).
    try:
        if docker:
            sandbox.docker_precondition_check()
        else:
            sandbox.smoke_test()
    except sandbox.SandboxUnavailable as exc:
        output.error(str(exc).splitlines()[0])
        raise typer.Exit(code=1)

    # Phase 0b — executable and served-model preflight, for the backends that
    # drive an operator-served model (`opencode`, and `local`, its older
    # name). First the opencode binary, resolved the way the runner will
    # resolve it (PATH, then the installer's `~/.opencode/bin`): a standard
    # install is off a non-login PATH, and finding that out at
    # `subprocess.Popen` meant a raw traceback after every other preflight
    # had passed. Then one cheap /models request. Both run before the flock
    # and before any bd read, so a missing binary, a dead endpoint, or a
    # mis-served model leaves the tracker untouched and no claim is burned
    # on a worker that could only fail. The server is operator-managed: no
    # retry loop, and no tool-calling probe here (the worker's own CodeGraph
    # handshake proves that). The message names the backend that was
    # launched, so an operator who wrote `local` is not sent to fix an
    # `opencode` run. CodeGraph policy was enforced at the probe above,
    # which for these backends includes the `opencode.json` registration
    # the worker would otherwise discover by failing its handshake after a
    # claim.
    if local_config is not None:
        try:
            opencode_binary = resolve_opencode_binary()
        except OpenCodeBinaryError as exc:
            output.error(f"{resolved_backend} backend: {exc}", hint=exc.remediation)
            raise typer.Exit(code=1)
        output.progress("grind", f"opencode binary: {opencode_binary}")
        try:
            probe_models(local_config)
        except LocalServerError as exc:
            output.error(f"{resolved_backend} backend: {exc}", hint=exc.remediation)
            raise typer.Exit(code=1)
        output.progress("grind", f"local server reachable: {local_config.display}")

    # Phase 1 — hook precheck (must run BEFORE any claude spawn).
    if resolved_backend == "claude":
        try:
            hooks.check_hooks_enabled(target)
        except hooks.HookConflictError as exc:
            output.error(str(exc).splitlines()[0])
            raise typer.Exit(code=1)

    # Phase 2 — flock so two grinds can't race for the same repo.
    try:
        with grind_flock(target), ExitStack() as gate_cleanup:
            gate_run_id = uuid4()
            baseline_backend = resolved_backend
            route_overrides = RouteOverrides(
                backend, implement_model, implement_reasoning_effort,
                verify_model, verify_reasoning_effort,
            )
            log = _log_path(target)
            write_log = _log_writer(log)
            write_log(
                "=== ortus grind started "
                f"(subprocess-per-task shape; backend={resolved_backend}; "
                f"verification={verification_note}; "
                f"prompt={prompt_variant_note}) ==="
            )
            if verification_text:
                write_log(
                    "verification: prototype bar — "
                    + _gate_summary(*prototype_gates)
                )
            write_log(f"profile: {implement_profile.display_name}")
            write_log(f"profile: {verify_profile.display_name}")
            write_log(f"profile: {finalize_profile.display_name}")
            # The commit-message model pass is retired (branch-scoped
            # candidates, commit B): the worker writes its message at commit
            # time. A leftover journal is not a resume key and is discarded
            # below rather than replayed through compose/finalize.
            output.progress("grind", f"starting; log → {log.relative_to(target)}")

            bd = _make_bd(target)
            git = _make_git(target)
            if not git.is_git_repo():
                output.error("grind: working tree is not a git repository")
                raise typer.Exit(code=1)
            # Re-assert branch discipline before anything else: a stray branch
            # left by a prior crashed grind (or a manual checkout) is caught
            # here and either re-checked-out or halted on, so we never start
            # spawning workers on top of stranded work (ortus-6fu6).
            _discard_leftover_journal(target, write_log)
            _enforce_branch_discipline(
                git,
                integration_branch,
                write_log,
                phase="startup",
            )
            # Leftover work is the leftover in_progress claim in bd plus the
            # git tree. A leftover journal is never the resume key.
            leftover_claims = bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS)
            if bind_worker and len(leftover_claims) > 1:
                for claimed_id in sorted(leftover_claims):
                    bd.add_label(claimed_id, "human")
                    bd.add_comment(claimed_id, "PLAN-GAP: multiple leftover claims require human handling")
                raise BackendError("multiple leftover claims require human handling")
            resume_issue_id = (
                next(iter(leftover_claims)) if len(leftover_claims) == 1 else None
            )
            if resume_issue_id is not None:
                write_log(
                    "recovery: resuming the single claimed issue "
                    f"{resume_issue_id}"
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
            # point is a leftover from a prior window: a prior grind claimed
            # it and the worker left it unfinished. Per-iteration orphan
            # detection (compute_delta on the before/after diff) can never
            # see these because they sit in `before.in_progress_ids` and get
            # subtracted out of every later delta.
            #
            # f2he.2: a live unfinished claim is not an orphan. Revert is
            # remapped to warn so leftover in_progress stays the next
            # window's goal. Escalate still hands the issue to a human when
            # the operator asked for that policy.
            orphan_ids = set(initial_snapshot.in_progress_ids)
            if orphan_ids:
                write_log(
                    f"startup leftover claim(s): {len(orphan_ids)} "
                    f"in_progress issue(s) left for the next window: "
                    f"{sorted(orphan_ids)}"
                )
                # f2he.2: a live unfinished claim is not an orphan. Revert
                # must not fire. Escalate still hands the issue to a human
                # when the operator asked for that policy.
                action = apply_orphan_policy(
                    (
                        OrphanPolicy.WARN
                        if orphan_policy is OrphanPolicy.REVERT
                        else orphan_policy
                    ),
                    orphan_ids,
                    revert_fn=lambda i: bd.update_status(i, "open"),
                    escalate_fn=lambda i: bd.add_label(i, "human"),
                )
                for line in action.actions_taken:
                    write_log(f"  orphan-policy: {line}")
                if (
                    action.policy is OrphanPolicy.ESCALATE
                    and resume_issue_id is not None
                    and resume_issue_id in orphan_ids
                ):
                    # Escalation hands the issue to a human, so resuming it
                    # here would walk straight back into the agent loop the
                    # operator just took it out of. Drop only the routing hint:
                    # the uncommitted work stays in the tree, untouched.
                    write_log(
                        f"startup orphan sweep: {resume_issue_id} was escalated to the "
                        "human queue; not resuming it. Its uncommitted work stays in "
                        "the worktree untouched"
                    )
                    resume_issue_id = None
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

            if resume_issue_id is not None:
                # A leftover resume names its issue directly, bypassing the
                # label filter every snapshot gate applies. Feeding an excluded
                # issue to a worker arms a trap: the worker runs, verification
                # cannot see the claim, and the finished candidate is silently
                # dropped (ortus-lf02). Skip the resume loudly instead — no
                # worker ever runs for a hidden claim; its claim stays parked
                # and the queue continues past it.
                excluded = _claim_excluded_labels(bd, resume_issue_id)
                if excluded:
                    skip_note = (
                        f"not resuming {resume_issue_id}: it carries the "
                        f"excluded label(s) {', '.join(excluded)}, so no worker "
                        "may run for it — every snapshot gate would ignore its "
                        "claim and a finished candidate would be silently "
                        "dropped. Its claim and work stay parked; "
                        "read the issue's newest comment, decide, and relabel "
                        "it for the queue. The queue continues past it."
                    )
                    write_log(f"startup: {skip_note}")
                    output.warn(skip_note)
                    resume_issue_id = None

            # Wedged-claim escalation (ortus-v8x8): a leftover claim that has
            # already burned the threshold of consecutive resumed windows —
            # each ending still in_progress with no new commits on the
            # integration branch — goes to the human queue instead of another
            # worker. The label (the same convention the readiness gate uses)
            # keeps it out of every snapshot; the claim itself stays
            # in_progress so the worker-tree context is preserved for the
            # operator.
            resume_no_close_count = 0
            # One process now spans many windows, so more than one claim can
            # reach the threshold before the run ends; every escalation keeps
            # its own end-of-run hint.
            escalated_claims: list[tuple[str, int]] = []
            #: The re-plan directive the next window's prompt carries, set by
            #: the stuck decision that left the claim resumable. Startup has
            #: no verdict of its own, so it never opens with one.
            replan_directive = ""
            if resume_issue_id is not None:
                resume_no_close_count = _no_close_window_count(bd, resume_issue_id)
                # No window has run in this process yet, so there is no
                # verdict to read: this call always fails open to the
                # threshold rule a leftover claim met before grind restarted.
                _, startup_action = _decide_stuck(
                    bd, target, judge_config, gate_run_id, resume_issue_id,
                    None, resume_no_close_count, False, write_log,
                )
                if startup_action is StuckAction.ESCALATE:
                    _escalate_wedged_claim(
                        bd,
                        resume_issue_id,
                        resume_no_close_count,
                        integration_branch,
                        write_log,
                        log_context="startup",
                    )
                    escalated_claims.append(
                        (resume_issue_id, resume_no_close_count)
                    )
                    resume_issue_id = None
                    resume_no_close_count = 0
                    # Re-snapshot so queue_drained and the loop's first
                    # `before` see post-escalation state (the human label
                    # trims the claim from the excluded counts).
                    initial_snapshot = _snapshot(bd)

            if queue_drained(initial_snapshot):
                write_log("queue already drained; nothing to do.")
                output.progress("grind", "queue already drained; nothing to do.")
                for escalated in escalated_claims:
                    _announce_wedged_escalation(escalated)
                return

            # Phase 3 — cache env vars (relocate ~/.cache into project-local).
            cache.ensure_cache_dirs(target)
            cache_env = cache.env_overrides(target)
            # Preserve the zero-argument seam used by existing Claude test and
            # plugin overrides. Any non-Claude backend (codex, grok, local) is
            # passed through with the repo so make_runner can return the
            # matching sibling type and, for local, read the [local] table.
            runner = (
                _make_runner()
                if resolved_backend == "claude"
                else _make_runner(resolved_backend, repo=target)
            )
            configure_codegraph = getattr(runner, "configure_codegraph", None)
            if callable(configure_codegraph):
                configure_codegraph(codegraph_probe.capability)
            runner.extra_env.update(cache_env)
            # Workers run in disposable clones but share the primary
            # repository's tracker: BEADS_DIR pins every bd command to the one
            # database, so intake and workers never fork state.
            runner.extra_env.setdefault(
                "BEADS_DIR", str((target / ".beads").resolve())
            )

            tasks_completed = 0
            iters_run = 0
            # Console-only dedupe for readiness-skip warnings, keyed on issue
            # id plus summary text so a work spec that re-fails differently warns
            # again. The log keeps every occurrence.
            warned_unready: set[tuple[str, str]] = set()
            # Wedged-claim tracking for the (single) resumed window: the claim
            # id and the integration-branch tip at spawn, so the post-window
            # judgment can tell "progressing" (HEAD advanced) from "wedged".
            resuming = False
            resumed_claim_id = ""
            resumed_tip = ""

            while True:
                gate_turn = None
                shadow_decision_id = None
                # Milestone rollover: an epic whose children are all closed
                # is finished work, not a claimable unit — close it here so
                # the next milestone's subtree unblocks and this iteration
                # can claim from it. Must precede the `before` snapshot.
                _rollover_exhausted_epics(bd, write_log)
                before = _snapshot(bd)
                # Closed ids are captured so post-iteration attribution can
                # name a worker that claimed AND closed within one window —
                # the in_progress diff is empty there and the snapshot's
                # closed count alone cannot say which issue landed.
                before_closed_ids: set[str] = (
                    bd.closed_ids() if harness_select else set()
                )
                # Until a claim materializes a worker workspace, every phase
                # operates on the primary repository (legacy --condition mode
                # never leaves it).
                worker_repo = target
                implementation_probe = codegraph_probe
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
                    git,
                    integration_branch,
                    write_log,
                    phase="pre-iter",
                )

                # Queue reads can auto-export generated Beads state between
                # iterations. Checkpoint that state. A dirty tree is allowed —
                # the worker sees it via goal-prompt; grind does not snapshot
                # candidate paths into a journal.
                if resolved_backend == "codex":
                    _checkpoint_codex_preflight(
                        git,
                        integration_branch,
                        write_log,
                        accept_baseline=True,
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
                        ready = (
                            [bd.show(resume_issue_id)]
                            if resume_issue_id is not None
                            else bd.list_ready(exclude_labels=EXCLUDED_LABELS)
                        )
                    except Exception as exc:  # bd hiccup: don't crash the loop
                        write_log(f"iter prep: bd ready failed ({exc}); idle-sleeping")
                        if idle_sleep > 0:
                            time.sleep(idle_sleep)
                        continue
                    # `bd ready` can return a compact projection. Load each
                    # authoritative work spec before the readiness guard decides
                    # whether a fast implementer may claim it.
                    ready_packets: list[dict] = []
                    for candidate in ready:
                        if (
                            str(
                                candidate.get("issue_type")
                                or candidate.get("type")
                                or ""
                            )
                            .strip()
                            .lower()
                            == "epic"
                        ):
                            ready_packets.append(candidate)
                            continue
                        candidate_id = str(candidate.get("id") or "").strip()
                        if not candidate_id:
                            message = "readiness skip: ready entry has no issue id"
                            write_log(message)
                            output.warn(message)
                            continue
                        try:
                            ready_packets.append(bd.show(candidate_id))
                        except Exception as exc:
                            message = (
                                f"readiness skip: {candidate_id}: could not load full "
                                f"work spec ({exc})"
                            )
                            write_log(message)
                            output.warn(message)

                    unready: list[ReadinessReport] = []
                    unready_titles: dict[str, str] = {}

                    def report_unready(
                        candidate: dict, report: ReadinessReport
                    ) -> None:
                        unready.append(report)
                        title = str(candidate.get("title") or "").strip()
                        unready_titles[report.issue_id] = title
                        if enforce_judge:
                            # A ready leaf later in this queue wins without
                            # changing unrelated unready packets this iteration.
                            return
                        diagnostic = report.diagnostic()
                        write_log(
                            f"readiness skip (labeled human for repair): "
                            f"{diagnostic}"
                        )
                        # Label at skip time, not only when nothing is
                        # claimable: the worker selects from its own
                        # `bd ready`, and only the label keeps a skipped
                        # leaf out of that view (ortus-ts3z).
                        _flag_unready_for_human(bd, [report], write_log)
                        key = (report.issue_id, report.summary())
                        if key in warned_unready:
                            return
                        warned_unready.add(key)
                        output.warn(
                            f"{_unready_skip_line(title, report)}. It stays "
                            "open, labeled human, and out of the queue. Run "
                            "ortus plan or edit the work spec; repair alone "
                            "does not re-queue it — also run: bd label remove "
                            f"{report.issue_id} human."
                        )

                    if resume_issue_id is not None:
                        # A resume continues an existing claim. The readiness
                        # guard governs FRESH claims only: routing a leftover
                        # in_progress through it would flag that work human
                        # instead of continuing it.
                        target_issue = ready_packets[0] if ready_packets else None
                    else:
                        target_issue = select_ready_issue(
                            ready_packets, on_unready=report_unready
                        )

                    # Every unready leaf was already labeled human at skip
                    # time inside report_unready. Epics never populate
                    # ``unready`` (select_ready_issue skips them without
                    # reporting), and a leftover in_progress resume never
                    # does either. What remains is the no-ready-issue exit
                    # when nothing at all was claimable.
                    if target_issue is None:
                        if enforce_judge:
                            _flag_unready_for_human(bd, unready, write_log)
                        # Queue is non-empty (not drained) but nothing is ready —
                        # everything left is blocked or human-flagged. We hold the
                        # flock, so no other actor will unblock it; stop rather
                        # than spin spawning workers that have nothing to do.
                        write_log(
                            "no ready issue to claim (queue blocked or human-only); "
                            f"exiting outer loop. tasks_completed={tasks_completed}"
                        )
                        for report in unready:
                            diagnostic = f"readiness: {report.diagnostic()}"
                            follow_up = (
                                f"follow-up: bd update {report.issue_id} "
                                "--description/--design/--acceptance to readiness "
                                f"schema v1, then: bd label remove "
                                f"{report.issue_id} human, then re-run: "
                                f"ortus grind {target}"
                            )
                            write_log(diagnostic)
                            write_log(follow_up)
                            # The exit listing is the run's explanation, so it
                            # always prints regardless of the warn dedupe — but
                            # at summary altitude; the log keeps the detail.
                            output.error(
                                "readiness: "
                                + _unready_skip_line(
                                    unready_titles.get(report.issue_id, ""),
                                    report,
                                )
                            )
                            output.error(follow_up)
                        if unready:
                            # One pointer for the whole listing: the verdict
                            # above is what `ortus validate` prints without
                            # a run, so a repair can be checked before the
                            # next grind instead of at its claim.
                            output.error(
                                "readiness: re-check after repair, without a "
                                f"run: ortus validate {target} "
                                + " ".join(report.issue_id for report in unready)
                            )
                        break
                    issue_id = target_issue["id"]
                    # f2he.2: grind does not claim a fresh ready issue. The
                    # worker claims via goal-prompt. A leftover in_progress
                    # is already claimed; spawn a new process for it.
                    resuming = resume_issue_id is not None
                    # Consume the directive the previous window's decision
                    # left, the same way the resume id itself is consumed: one
                    # window carries it, and a window that is not that resume
                    # never inherits it.
                    iteration_replan = replan_directive if resuming else ""
                    replan_directive = ""
                    if resuming:
                        resumed_claim_id = issue_id
                        resumed_tip = git.branch_tip(integration_branch)
                        write_log(
                            f"iter prep: continuing leftover claim {issue_id}"
                        )
                    else:
                        write_log(
                            f"iter prep: worker will claim {issue_id} via goal-prompt"
                        )
                    target_issue = bd.show(issue_id)
                    semantic_advice = readiness_context(
                        evaluate_readiness(target, target_issue, judge_config)
                    )
                    if judge_config.enabled and judge_config.mode == JudgeMode.SHADOW:
                        shadow_decision_id = _shadow_turn(
                            packet=target_issue, repo=target, config=config,
                            judge_config=judge_config, baseline=baseline_backend,
                            overrides=route_overrides, run_id=gate_run_id,
                        )
                    if enforce_judge:
                        gate_turn, bundle = _gate_turn(
                            bd=bd, issue_id=issue_id, repo=target, config=config,
                            judge_config=judge_config, baseline=baseline_backend,
                            overrides=route_overrides, codegraph_mode=codegraph_mode,
                            adapter=codegraph_adapter, extra_env=cache_env,
                            goal_template=goal_template, cleanup=gate_cleanup,
                            run_id=gate_run_id,
                        )
                        if bundle is None:
                            break
                        target_issue = gate_turn.bound.issue
                        runner = bundle.runner
                        resolved_backend = bundle.execution_backend or bundle.backend.value
                        implement_profile = bundle.implement_profile
                        verify_profile = bundle.verify_profile
                        finalize_profile = bundle.finalize_profile
                        codegraph_probe = bundle.codegraph_probe
                    elif judge_config.pre_tool:
                        gate_turn = _GateTurn(prepare_bound_issue(
                            bd, issue_id, goal_template=goal_template,
                        ))
                        gate_cleanup.callback(gate_turn.cleanup, bd)
                        target_issue = gate_turn.bound.issue
                        runner.extra_env = gate_turn.bound.worker_env(runner.extra_env)
                    if judge_config.pre_tool:
                        check_pre_tool(target, resolved_backend, docker=docker)
                    # f2he.4: work on the primary checkout (main). Do not cut
                    # ortus/<id> or clone logs/grind-workspaces/<id>.
                    if git.has_commits():
                        current = git.current_branch()
                        if current != integration_branch:
                            write_log(
                                f"iter prep: HALT — working tree is on "
                                f"{current or 'a detached HEAD'}, not "
                                f"{integration_branch}"
                            )
                            output.error(
                                f"grind: working tree is on "
                                f"{current or 'a detached HEAD'}, not "
                                f"{integration_branch}",
                                hint="commit or stash your work, check out "
                                f"{integration_branch}, then re-run grind",
                            )
                            raise typer.Exit(code=1)
                    worker_repo = target
                    resume_issue_id = None
                    configure_codegraph = getattr(runner, "configure_codegraph", None)
                    if callable(configure_codegraph):
                        configure_codegraph(codegraph_probe.capability)
                    implementation_probe = codegraph_probe
                    if callable(configure_codegraph):
                        configure_codegraph(implementation_probe.capability)
                    implementation_instruction = _IMPLEMENTATION_INSTRUCTION
                    iteration_lessons = _selected_lessons(bd, write_log)
                    iteration_lessons_text = _lessons_section(iteration_lessons)
                    try:
                        iteration_prompt = _compose_work_prompt(
                            work_template,
                            target_issue,
                            resolved_backend,
                            phase_instruction=implementation_instruction,
                            phase_contract_text=phase_contract(
                                CodeGraphPhase.IMPLEMENTATION, implementation_probe
                            ),
                            verification_text=verification_text,
                            lessons_text=iteration_lessons_text,
                            bound_issue_id=issue_id if gate_turn else None,
                            goal_template=goal_template if gate_turn else None,
                            semantic_advice=semantic_advice,
                            stable_prefix=stable_prefix,
                            replan_text=iteration_replan,
                        )
                    except BackendError as exc:
                        write_log(f"iter prep: HALT — {exc}")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                    conflict = _stale_completion_contract_diagnostic(
                        iteration_prompt, repo=target, audited=prompt_audit
                    )
                    if conflict:
                        write_log(f"iter prep: HALT — {conflict}")
                        output.error(conflict)
                        raise typer.Exit(code=1)
                    _log_dropped_lessons(
                        iteration_lessons,
                        iteration_lessons_text,
                        iteration_prompt,
                        write_log,
                    )
                    write_log(
                        f"iter {iters_run + 1}: goal-prompt ready for {issue_id} "
                        f"({resolved_backend})"
                    )
                    # output.progress escapes markup itself, so a bracketed
                    # title survives the console without pre-escaping. Grind
                    # never claims here, so the console predicts rather than
                    # asserts; the id the worker actually claimed is read
                    # back from bd after the iteration (ortus-ts3z).
                    iter_title = target_issue.get("title") or "untitled"
                    if gate_turn:
                        output.progress("grind", f"judge bound worker to {issue_id} ({resolved_backend})")
                    elif resuming:
                        output.progress(
                            "grind",
                            f'continuing leftover claim "{iter_title}" '
                            f"({issue_id})",
                        )
                    else:
                        output.progress(
                            "grind",
                            "worker will claim from the ready queue — "
                            f'readiness-passing head is "{iter_title}" '
                            f"({issue_id})",
                        )
                else:
                    iteration_prompt = _legacy_prompt(condition, resolved_backend)

                if gate_turn:
                    gate_turn.current_issue(bd)
                # A stuck-but-alive worker would otherwise block the entire
                # loop forever (only a human kill recovers it). --worker-timeout
                # hard-caps the iteration: on exceed the runner SIGTERM/SIGKILLs
                # the worker's process group, we log it distinctly, and fall
                # through to the SAME post-iteration recovery as a clean exit —
                # bd state is ground truth, so a worker that closed its issue
                # then hung still counts, and a claimed-but-unclosed issue still
                # gets the orphan-policy treatment.
                # Re-armed every iteration so resume-from-captured can skip a
                # worker spawn; leftover in_progress still runs one.
                implementation_worker_ran = True
                worker_timed_out = False
                post_start_tip = (
                    git.branch_tip(integration_branch) if judge_config.post_turn else None
                )
                phase_offset = log.stat().st_size if log.exists() else 0
                impl_started = time.monotonic()
                impl_handshake_logged = False
                implementation_summary = parse_transcript(
                    log,
                    phase=CodeGraphPhase.IMPLEMENTATION,
                    probe=implementation_probe,
                    start_offset=phase_offset,
                )

                def _poll_impl_handshake() -> None:
                    nonlocal impl_handshake_logged, implementation_summary
                    implementation_summary = parse_transcript(
                        log,
                        phase=CodeGraphPhase.IMPLEMENTATION,
                        probe=implementation_probe,
                        start_offset=phase_offset,
                    )
                    if (
                        implementation_summary.capability_observed
                        and not impl_handshake_logged
                    ):
                        impl_handshake_logged = True
                        write_log("implementation CodeGraph handshake succeeded")
                        _append_handshake(
                            log, CodeGraphPhase.IMPLEMENTATION, success=True
                        )

                hook_run = None
                try:
                    if judge_config.pre_tool:
                        hook_run = HookRun(
                            target, issue_id, judge_config, str(gate_run_id), runner,
                        )
                    if implementation_probe.available:
                        write_log("implementation CodeGraph handshake requested")
                    else:
                        output.progress(
                            "grind",
                            "implementation CodeGraph handshake fallback active",
                        )
                    # Claude and grok both answer to a /goal Stop hook that
                    # re-prompts them until the issue is closed and in sync.
                    # A worker that closed and pushed, or that flagged its
                    # claim human and tried to stop, therefore keeps
                    # answering until the watchdog fires. The harness reaps
                    # it instead, within one poll, and the log names the
                    # reason rather than a timeout. Codex runs its own turn
                    # loop and exits by itself; its termination stays as is.
                    reap_when = None
                    reap_reasons: list[str] = []
                    flagged_at_start: frozenset[str] | None = None
                    human_open_at_start: frozenset[str] | None = None
                    if resolved_backend in ("claude", "grok"):
                        try:
                            baseline_closed = bd.count_by_status("closed")
                        except Exception:
                            baseline_closed = None
                        try:
                            flagged_at_start = frozenset(_flagged_claims(bd))
                        except Exception:
                            flagged_at_start = None
                        # The operator's open issues, remembered so a claim
                        # the worker puts on one of them can be handed back
                        # once the flagged-claim reap has fired.
                        try:
                            human_open_at_start = frozenset(
                                bd.open_ids(labels=EXCLUDED_LABELS)
                            )
                        except Exception:
                            human_open_at_start = None

                        def _reap_worker() -> bool:
                            if hook_run is not None and hook_run.poll():
                                return True
                            reason = _reap_reason(
                                bd,
                                git,
                                baseline_closed=baseline_closed,
                                flagged_at_start=flagged_at_start,
                                integration_branch=integration_branch,
                                bound_issue_id=gate_turn.bound.issue["id"] if gate_turn else None,
                            )
                            if reason is None:
                                return False
                            write_log(f"iter {iters_run}: {reason}; reaping worker")
                            reap_reasons.append(reason)
                            return True

                        reap_when = _reap_worker
                    if gate_turn:
                        gate_turn.current_issue(bd)
                        gate_turn.launched = True
                    iters_run += 1
                    write_log(
                        f"iter {iters_run}: spawning {resolved_backend} "
                        "(single-issue worker)"
                    )
                    rc = runner.run(
                        iteration_prompt,
                        repo=worker_repo,
                        log_path=log,
                        fast=fast,
                        profile=implement_profile,
                        timeout=(worker_timeout if worker_timeout > 0 else None),
                        reap_when=reap_when,
                        on_poll=_poll_impl_handshake,
                    )
                except subprocess.TimeoutExpired:
                    worker_timed_out = True
                    rc = 143  # 128 + SIGTERM; group was SIGTERM'd then SIGKILL'd
                    write_log(
                        f"iter {iters_run}: worker TIMEOUT after {worker_timeout}s, "
                        f"killed (rc={rc})"
                    )
                finally:
                    if hook_run is not None:
                        try:
                            hook_run.poll()
                            hook_run.escalate(bd)
                        finally:
                            hook_run.close()
                    if shadow_decision_id is not None:
                        _shadow_outcome(
                            bd=bd, repo=target, config=judge_config, run_id=gate_run_id,
                            decision_id=shadow_decision_id, observed_id=issue_id,
                            before_claims=before.in_progress_ids,
                            before_closed=before_closed_ids,
                            resumed_id=issue_id if resuming else None,
                            worker_ms=elapsed_ms(impl_started),
                        )
                    if gate_turn and gate_turn.launched:
                        gate_turn.record_outcome(
                            bd, target, judge_config, gate_run_id, elapsed_ms(impl_started),
                        )
                if hook_run is not None and hook_run.requested:
                    write_log(f"iter {iters_run}: judge pre_tool human; worker reaped")
                    output.progress("grind", f"judge requires human handling for {issue_id}: needs_human")
                    break
                if (
                    reap_reasons
                    and reap_reasons[-1].startswith(_FLAGGED_REASON)
                    and flagged_at_start is not None
                ):
                    # The worker is dead, so bd now shows the post-mortem
                    # state: any claim it put on an issue that was the
                    # operator's before the window began goes back, and what
                    # it parked on its own is read for a pin it could have
                    # made instead.
                    handed_back: set[str] = set()
                    if gate_turn is None:
                        handed_back = _hand_back_misclaims(
                            bd,
                            flagged_at_start=flagged_at_start,
                            human_open_at_start=human_open_at_start,
                            window=iters_run,
                            write_log=write_log,
                        )
                    try:
                        parked = _flagged_claims(bd) - flagged_at_start - handed_back
                    except Exception as exc:
                        parked = set()
                        write_log(
                            f"iter {iters_run}: pin-skew: could not read the "
                            f"parked claims ({exc})"
                        )
                    tag_pin_skew_claims(
                        bd, parked, window=iters_run, write_log=write_log
                    )
                if resolved_backend == "claude":
                    rejection = _claude_goal_rejection(log, start_offset=phase_offset)
                    if rejection is not None:
                        write_log(
                            f"iter {iters_run}: HALT — Claude rejected /goal before "
                            f"running a worker turn: {rejection}"
                        )
                        output.error(
                            "grind: Claude rejected the /goal condition before worker work",
                            hint=rejection,
                        )
                        raise typer.Exit(code=1)

                # How this window failed, named once from what the launch
                # already reported and what the worker already logged. The
                # marker is a plain ortus line, so every surface that reads
                # these logs keeps rendering it as text, and `ortus cost`
                # parses it into per-backend, per-model rates. A window that
                # ended well is not classified at all.
                window_failure = classify_worker_window(
                    exit_code=rc,
                    timed_out=worker_timed_out,
                    log_path=log,
                    start_offset=phase_offset,
                )
                if window_failure is not None:
                    write_log(
                        failure_log_line(
                            window_failure,
                            iteration=iters_run,
                            backend=resolved_backend,
                            model=implement_profile.model,
                        )
                    )
                    if window_failure.unclassified:
                        output.warn(
                            f"worker window {iters_run} failed with no known "
                            "signal; counted as an Ortus harness bug"
                        )

                # Live implementation handshake is judged here, before f2he.2
                # reads bd status. Worker process exit is not a CodeGraph signal.
                if implementation_worker_ran:
                    _poll_impl_handshake()
                    append_normalized(log, implementation_summary)
                    if (
                        not implementation_summary.capability_observed
                        and codegraph_mode is not CodeGraphMode.OFF
                    ):
                        output.progress(
                            "grind",
                            "implementation CodeGraph fallback: "
                            + "; ".join(implementation_summary.fallbacks[:3]),
                        )
                    write_log(
                        "CodeGraph implementation summary: "
                        f"queries={len(implementation_summary.events)} "
                        f"fallbacks={implementation_summary.fallbacks or 'none'}"
                    )
                    try:
                        require_handshake(implementation_summary)
                    except CodeGraphUnavailable as exc:
                        output.error(str(exc))
                        raise typer.Exit(code=1)

                # f2he.2: the iteration result is observable bd status only.
                # Do not re-run tests, do not require Claims v1, do not
                # spawn a verifier or a correction, do not revert a live
                # in_progress claim.
                closed_delta = 1
                if harness_select:
                    # Attribution reads bd, not grind's prediction: the ids
                    # that turned in_progress during the window are the
                    # worker's actual claim. A claim closed within the same
                    # window leaves that diff empty, so the closed-id delta
                    # names it instead (ortus-ts3z).
                    after_state = _snapshot(bd)
                    claimed_ids = sorted(
                        after_state.in_progress_ids - before.in_progress_ids
                    )
                    if not claimed_ids and after_state.closed > before.closed:
                        claimed_ids = sorted(bd.closed_ids() - before_closed_ids)
                    if gate_turn:
                        judged_id = gate_turn.bound.issue["id"]
                    elif claimed_ids:
                        attribution = ", ".join(claimed_ids)
                        write_log(
                            f"iter {iters_run}: worker claimed {attribution} "
                            "(read back from bd state)"
                        )
                        output.progress("grind", f"worker claimed {attribution}")
                        judged_id = claimed_ids[0]
                    else:
                        write_log(
                            f"iter {iters_run}: no worker claim observed in bd; "
                            f"judging the readiness-passing head {issue_id}"
                        )
                        judged_id = issue_id
                    try:
                        judged = bd.show(judged_id)
                    except Exception:
                        judged = {}
                    judged_status = str(judged.get("status") or "open")
                else:
                    after_state = _snapshot(bd)
                    iter_delta = compute_delta(before, after_state)
                    closed_delta = max(iter_delta.closed_delta, 0)
                    if iter_delta.closed_one_or_more:
                        judged_status = "closed"
                        judged_id = "issue"
                    elif after_state.in_progress_ids:
                        judged_status = "in_progress"
                        judged_id = sorted(after_state.in_progress_ids)[0]
                    else:
                        judged_status = "open"
                        judged_id = "issue"
                post_stop = False
                # The verdict outlives `apply_outcome` because the stuck
                # decision below reads the same answer: its confidence shapes
                # a vector there rather than parking the bead here.
                post_verdict: OutcomeVerdict | None = None
                if judge_config.post_turn and harness_select and implementation_worker_ran:
                    try:
                        # Attribution and the required handshake precede advisory judgment.
                        packet = bd.show(judged_id)
                        post_end_tip = git.branch_tip(integration_branch)
                        observation = WorkerOutcome(
                            exit_status=rc, watchdog=worker_timed_out,
                            observed_status=OutcomeStatus(packet.get("status", "unknown")),
                            branch_advanced=bool(
                                post_start_tip
                                and post_end_tip and post_end_tip != post_start_tip
                            ),
                            # Named once above; the record carries that class
                            # rather than the judge deriving a second opinion.
                            worker_failure=(
                                window_failure.failure
                                if window_failure is not None else None
                            ),
                        )
                        verdict = evaluate_outcome(packet, observation, judge_config)
                        post_verdict = verdict
                        post_stop = apply_outcome(
                            bd, target, judged_id, observation, verdict,
                            judge_config, gate_run_id,
                        )
                    except Exception:  # noqa: BLE001 - preserve state, never echo provider/tracker text
                        output.progress("grind", "judge post_turn unavailable; preserving worker result")
                        # A broken log or tracker cannot authorize another worker window.
                        post_stop = judge_config.mode == JudgeMode.ENFORCE
                if judged_status == "closed":
                    tasks_completed += closed_delta
                    write_log(
                        f"iter {iters_run}: worker closed {judged_id} "
                        f"(tasks_completed={tasks_completed})"
                    )
                    output.progress(
                        "grind",
                        f"closed {judged_id} — {tasks_completed} done this run",
                    )
                    if tasks > 0 and tasks_completed >= tasks:
                        write_log(
                            f"--tasks cap reached: {tasks_completed}/{tasks}; "
                            "exiting outer loop"
                        )
                        break
                elif judged_status == "in_progress":
                    # Wedged-claim counter (ortus-v8x8): a RESUMED window that
                    # ends with the claim still in_progress either advanced
                    # the integration branch (progressing — reset) or didn't
                    # (wedged — count it, so the next resume can escalate at
                    # the threshold). Fresh claims are outside the counter;
                    # their first no-close window is resumed by the next
                    # iteration below, which counts from there.
                    window_advanced = False
                    if resuming and judged_id == resumed_claim_id:
                        head_now = git.branch_tip(integration_branch)
                        window_advanced = bool(head_now and head_now != resumed_tip)
                        if window_advanced:
                            if resume_no_close_count:
                                _record_no_close_window(
                                    bd, judged_id, 0, write_log
                                )
                            write_log(
                                f"iter {iters_run}: resumed window advanced "
                                f"{integration_branch}; no-close counter reset"
                            )
                        else:
                            burned = resume_no_close_count + 1
                            _record_no_close_window(
                                bd, judged_id, burned, write_log
                            )
                            write_log(
                                f"iter {iters_run}: resumed window ended with "
                                "no close and no new commits on "
                                f"{integration_branch} (no-close window "
                                f"{burned} of {_WEDGED_WINDOW_THRESHOLD})"
                            )
                    elif post_start_tip is not None:
                        # A fresh claim keeps no counter, but the decision
                        # below still wants to know whether the window it just
                        # spent moved the integration branch at all.
                        end_tip = git.branch_tip(integration_branch)
                        window_advanced = bool(end_tip and end_tip != post_start_tip)
                    # A claim that outlived its window is routed here and now:
                    # the human queue takes it at the threshold, an excluded
                    # label parks it, and anything else is resumed by the next
                    # iteration of this same process.
                    pending = _no_close_window_count(bd, judged_id)
                    blocked = _claim_excluded_labels(bd, judged_id)
                    _, stuck_action = _decide_stuck(
                        bd, target, judge_config, gate_run_id, judged_id,
                        post_verdict, pending, window_advanced, write_log,
                    )
                    if post_stop:
                        # A post-turn answer no longer halts the loop on its
                        # own. It is one input to the decision just taken,
                        # which has already chosen what this claim gets next —
                        # including the human queue, when that is what the
                        # vector argued for.
                        write_log(
                            f"iter {iters_run}: judge post_turn would have "
                            "withheld another window; the stuck decision "
                            f"({stuck_action.value}) governs {judged_id}"
                        )
                    if stuck_action is StuckAction.ESCALATE:
                        _escalate_wedged_claim(
                            bd,
                            judged_id,
                            pending,
                            integration_branch,
                            write_log,
                            log_context=f"iter {iters_run}",
                        )
                        escalated_claims.append((judged_id, pending))
                        resume_no_close_count = 0
                        replan_directive = ""
                    elif blocked:
                        # Feeding an excluded issue to a worker arms the
                        # ortus-lf02 trap: the worker runs, verification
                        # cannot see the claim, and a finished candidate is
                        # silently dropped. The queue continues past it — so
                        # the claim comes off too, or the issue sits
                        # in_progress forever with no window able to take it.
                        write_log(
                            f"iter {iters_run}: not resuming {judged_id}: it "
                            "carries the excluded label(s) "
                            f"{', '.join(blocked)}, so no worker may run for "
                            "it. Its work stays as it is and the queue "
                            "continues past it"
                        )
                        _release_parked_head(
                            bd,
                            judged_id,
                            blocked,
                            window=iters_run,
                            write_log=write_log,
                        )
                        resume_no_close_count = 0
                        replan_directive = ""
                    else:
                        resume_issue_id = judged_id
                        resume_no_close_count = pending
                        replan_directive = ""
                        if stuck_action is StuckAction.REPLAN:
                            # The directive is what makes the next window
                            # different from the one that stalled; the marker
                            # is what stops a third and fourth from being the
                            # same bet at the same odds.
                            spent = _replan_window_count(bd, judged_id) + 1
                            _record_replan_window(bd, judged_id, spent, write_log)
                            replan_directive = _replan_section(
                                pending, integration_branch
                            )
                            write_log(
                                f"iter {iters_run}: re-planning {judged_id} in "
                                f"a fresh window (replan {spent})"
                            )
                        write_log(
                            f"iter {iters_run}: left {judged_id} in_progress "
                            "for the next window"
                        )
                        # That next window is the next iteration of THIS
                        # process, not the next invocation: control falls
                        # through to the cap checks below and the loop keeps
                        # going, so one grind chews the whole backlog instead
                        # of dying on every no-close worker window
                        # (ortus-86ui).
                else:
                    write_log(
                        f"iter {iters_run}: WARN no bd-state change "
                        f"({judged_id} is {judged_status})"
                    )
                    if post_stop:
                        break
                    if idle_sleep > 0:
                        time.sleep(idle_sleep)
                    else:
                        break
                if iterations > 0 and iters_run >= iterations:
                    write_log(
                        f"--iterations cap reached: {iters_run}/{iterations}; "
                        "exiting outer loop"
                    )
                    break
                continue

            final_snapshot = _snapshot(bd)
            if resolved_backend == "codex":
                _checkpoint_codex_preflight(
                    git,
                    integration_branch,
                    write_log,
                    accept_baseline=True,
                )
            write_log(
                f"=== ortus grind ended; closed {tasks_completed} "
                f"(open: {initial_snapshot.open} → {final_snapshot.open}, "
                f"in_progress: {final_snapshot.in_progress}, "
                f"iters_run={iters_run}) ==="
            )
            leftover = final_snapshot.in_progress
            exit_in_progress, exit_open = _exit_counts(bd, final_snapshot)
            output.progress(
                "grind",
                f"done — {tasks_completed} landed this session, "
                f"{exit_in_progress} in_progress, {exit_open} open",
            )
            for escalated in escalated_claims:
                _announce_wedged_escalation(escalated)
            if leftover:
                output.progress(
                    "grind",
                    "the leftover claim outlived this run's stop condition "
                    "(a cap, a halt, or an escalation); resuming a leftover "
                    "claim is an iteration of the running grind, not a "
                    "re-invocation",
                )
    except FlockBusy as exc:
        output.error(str(exc), hint="another `ortus grind` is already running here")
        raise typer.Exit(code=1)
    except (BackendError, BdError, JudgeLogError, StateError, ProfileError) as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
