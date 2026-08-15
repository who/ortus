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
from pathlib import Path
from typing import Any, Callable, Optional

import typer
from rich.markup import escape as escape_markup

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
    CodeGraphProbe,
    CodeGraphUnavailable,
    append_normalized,
    parse_transcript,
    phase_contract,
    require_handshake,
)
from ortus.core.config import DEFAULT_MERGE_GATE_TIMEOUT, load_config
from ortus.core.profiles import AgentProfile, Phase, ProfileError
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
from ortus.core.repair import (
    RepairCreatedReplacements,
    guard_no_replacements,
    repair_readiness,
)
from ortus.core.repo import resolve_repo


_TRACKER_EXPORT_PATHS = frozenset(
    {
        ".beads/issues.jsonl",
        ".beads/interactions.jsonl",
    }
)


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


def _repair_context(bd: BdClient, reports: tuple[ReadinessReport, ...]) -> str:
    """Ground a grind-side repair in each work spec plus its parent epic.

    A grind run has no PRD, and guessing one would repair the work spec against
    the wrong document, so the parent epic is the only extra context the pass
    gets beyond the issue's own `bd show` output.
    """
    lines: list[str] = []
    for report in reports:
        try:
            parent = str(bd.show(report.issue_id).get("parent") or "").strip()
        except Exception:  # a bd hiccup must not cost us the whole pass
            parent = ""
        lines.append(
            f"- {report.issue_id} is a child of epic {parent}; read it with "
            f"`bd show {parent} --json` for the parent objective."
            if parent
            else f"- {report.issue_id} has no parent epic."
        )
    return (
        "CONTEXT. This pass runs inside `ortus grind`, which has no PRD. Repair "
        "each work spec from its own `bd show <id> --json` output and the parent "
        "epic named below; do not look for or invent a PRD.\n" + "\n".join(lines) + "\n"
    )


def _run_readiness_repair(
    bd: BdClient,
    reports: tuple[ReadinessReport, ...],
    *,
    repo: Path,
    log: Path,
    write_log: Callable[[str], None],
    backend: str,
    profile: AgentProfile,
    probe: CodeGraphProbe,
    timeout: int | None,
) -> tuple[int, object | None]:
    """Run one bounded readiness repair pass; return (exit code, transcript).

    Raises :class:`RepairCreatedReplacements` when the pass grew the queue
    instead of updating the work specs it was named — that is a hard error, not a
    skip, because silent queue growth is the failure mode the guard exists for.
    """
    repair_log = log.with_name(f"{log.stem}-repair{log.suffix}")
    ids_before = {issue["id"] for issue in bd.list_all()}
    try:
        rc = repair_readiness(
            repo,
            reports,
            log_path=repair_log,
            backend=backend,
            profile=profile,
            contract=phase_contract(CodeGraphPhase.PLANNING, probe),
            capability=probe.capability,
            context=_repair_context(bd, reports),
            timeout=timeout,
            runner_factory=_make_runner,
        )
    except subprocess.TimeoutExpired:
        write_log(f"readiness repair: TIMEOUT after {timeout}s; see {repair_log}")
        return 143, None
    if rc != 0:
        write_log(f"readiness repair: failed ({backend} exit {rc}); see {repair_log}")
        return rc, None
    guard_no_replacements(ids_before, {issue["id"] for issue in bd.list_all()})
    summary = parse_transcript(repair_log, phase=CodeGraphPhase.PLANNING, probe=probe)
    append_normalized(repair_log, summary)
    write_log(
        f"CodeGraph repair summary: queries={len(summary.events)} "
        f"fallbacks={summary.fallbacks or 'none'}"
    )
    return 0, summary


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
# done bar — not the inlined goal-prompt.md body.
_GOAL_POINTER = (
    "One window, one issue. Continue leftover in_progress, else run "
    "bd ready and claim the first non-epic. Read AGENTS.md. Follow "
    "`.ortus/prompts/goal-prompt.md` or `src/ortus/prompts/goal-prompt.md` "
    "if either exists. Session-close that id per AGENTS.md. "
    "Achieved when that issue is closed and HEAD is in sync with origin. "
    "The issue's criterion-check commands already ran during implement — "
    "they are the whole verification. Do not run pytest or the repo test "
    "suite after session-close. After session-close, answer with the id, "
    "close reason, HEAD sha, and the criterion-check commands that already "
    "passed, then stop. Do not re-read the implementation. Do not start "
    "another issue. Injected sections below are worker instructions, not "
    "extra achievement criteria."
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


def _lessons_contract(bd: BdClient, write_log: Callable[[str], None]) -> str:
    """The prior-lessons section of a worker's phase contract.

    A repository with no stored lessons injects nothing, and a failed tracker
    read degrades to the same empty section with a log line: a worker without
    memory is today's behavior and must remain viable. The readiness memory
    is excluded — bd already injects it into every session via priming.
    """
    try:
        lessons = bd.lessons(
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
        return ""
    return _lessons_section(lessons)


def _compose_work_prompt(
    template: str,
    issue: dict,
    backend: str = "claude",
    *,
    phase_instruction: str = "",
    phase_contract_text: str = "",
    lessons_text: str = "",
) -> str:
    """Build one backend-appropriate prompt for a single goal-prompt iteration.

    The /goal condition is ``_GOAL_POINTER`` (worker reads ``goal-prompt.md``
    from disk). Grind does not inject a claimed id. ``template`` and
    ``issue`` remain on the signature so existing callers keep compiling;
    neither is substituted into the prompt.

    ``lessons_text`` is the one optional section: when appending it would
    push the Claude ``/goal`` condition past the cap it is dropped rather
    than halting the run. The 4,000-character cap is Claude-only.
    """
    del template, issue

    task = _GOAL_POINTER
    if phase_instruction:
        task = phase_instruction.rstrip() + "\n\n" + task
    task += phase_contract_text
    wrap_limit = _CLAUDE_GOAL_CONDITION_LIMIT if backend == "claude" else None
    if (
        lessons_text
        and (wrap_limit is None or len(task) + len(lessons_text) <= wrap_limit)
    ):
        task += lessons_text
    if wrap_limit is not None and len(task) > wrap_limit:
        raise BackendError(
            "internal Claude /goal condition exceeds the 4,000-character limit "
            f"({len(task)} characters)"
        )
    return compose_worker_prompt(backend, task)  # type: ignore[arg-type]


def _done_bar_met(
    bd: BdClient,
    git: GitClient,
    baseline_closed: int,
    integration_branch: str,
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
        closed = bd.count_by_status("closed")
    except Exception:
        return None
    if closed > baseline_closed:
        return f"closed {baseline_closed}->{closed}"
    return None


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


def _console_safe(text: str) -> str:
    """Text a Rich console renders verbatim.

    A blocker now quotes git's own output, which may contain brackets — a hook
    printing `[ERROR] refused` would otherwise be read as markup and silently
    dropped from the very line that exists to explain the failure. The run log
    keeps the unescaped text.
    """

    return escape_markup(text)


def _unready_skip_line(title: str, report: ReadinessReport) -> str:
    """Console-altitude skip line: title first, id in parentheses, one clause.

    The full section-by-section enumeration keeps serving the log and the
    repair prompt; the console gets the summary a colleague would say aloud.
    """

    label = _console_safe(title) if title.strip() else report.issue_id
    return f'skipped "{label}" ({report.issue_id}) — {report.summary()}'


def _flag_unready_for_human(
    bd: BdClient,
    reports: list[ReadinessReport],
    write_log: Callable[[str], None],
) -> None:
    """Label each unready leaf human and comment the readiness diagnostic.

    Used only when the ready queue holds nothing implementable. A failed
    label add still warns and continues so every remaining id is attempted;
    grind never falls back to a repair worker from this path.
    """

    for report in reports:
        diagnostic = report.diagnostic()
        try:
            bd.add_label(report.issue_id, "human")
        except Exception as exc:
            write_log(
                f"readiness: could not label {report.issue_id} human ({exc})"
            )
            output.warn(
                f"could not label {report.issue_id} human ({exc}); "
                "leaving it open and stopping"
            )
        try:
            bd.add_comment(
                report.issue_id,
                "readiness schema v1 failed; grind will not repair this "
                f"packet.\n\n{diagnostic}",
            )
        except Exception as exc:
            write_log(
                f"readiness: could not comment on {report.issue_id} ({exc})"
            )
        write_log(f"readiness: flagged {report.issue_id} human")


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
    repair_unready: bool = typer.Option(
        False,
        "--repair-unready/--no-repair-unready",
        help=(
            "When the ready queue holds only tasks that fail readiness schema "
            "v1, default grind flags each one human and stops. "
            "--repair-unready opts into one planning-profile repair pass."
        ),
    ),
    repair_budget: int = typer.Option(
        2,
        "--repair-budget",
        help=(
            "Max readiness repair passes per grind run (0 disables). Bounds how "
            "much of the loop a badly planned queue can burn in repair "
            "subprocesses; each issue id is attempted at most once per run."
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
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="claude, codex, or grok; overrides ORTUS_BACKEND and .ortusrc.",
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
        config = load_config(repo=target)
        merge_gate, merge_gate_timeout = _resolve_merge_gate(config)
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
        # Repairing an unready work spec is authoring work, not implementation, so
        # the self-heal pass runs on the planning profile.
        plan_profile = config.resolve_profile(resolved_backend, Phase.PLAN)
        finalize_profile = config.resolve_profile(
            resolved_backend, Phase.FINALIZE
        )
    except (BackendError, ProfileError) as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)

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
    work_template = read_work_issue_condition() if harness_select else ""

    if dry_run:
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
        output.info(f"implement:      {implement_profile.display_name}")
        output.info(f"verify:         {verify_profile.display_name}")
        output.info(f"finalize:       {finalize_profile.display_name}")
        output.info(
            "repair:         "
            + (
                f"{plan_profile.display_name}, budget {repair_budget} pass(es)"
                if repair_unready and repair_budget > 0
                else "off (unready tasks are flagged human)"
            )
        )
        output.info(f"codegraph:      {codegraph_mode.value}")
        output.info(
            "merge-gate:     "
            + (
                f"on, timeout {int(merge_gate_timeout)}s"
                if merge_gate
                else "off"
            )
        )
        output.info(
            f"select:         {'worker (goal-prompt claim)' if harness_select else 'worker (legacy --condition)'}"
        )
        output.info("--- per-iteration prompt ---")
        if harness_select:
            output.info(
                _compose_work_prompt(
                    work_template,
                    {"id": "<ISSUE_ID>", "title": "<ISSUE_DETAILS>"},
                    resolved_backend,
                )
                + "\n(the worker orients, continues leftover in_progress or "
                "runs bd ready, and claims; grind only decides whether to spawn.)"
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
                try:
                    resumed_issue = bd.show(resume_issue_id)
                except Exception:
                    resumed_issue = {}
                excluded = sorted(
                    {str(label) for label in (resumed_issue.get("labels") or ())}
                    & set(EXCLUDED_LABELS)
                )
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

            if queue_drained(initial_snapshot):
                write_log("queue already drained; nothing to do.")
                output.progress("grind", "queue already drained; nothing to do.")
                return

            # Phase 3 — cache env vars (relocate ~/.cache into project-local).
            cache.ensure_cache_dirs(target)
            cache_env = cache.env_overrides(target)
            # Preserve the zero-argument seam used by existing Claude test and
            # plugin overrides. Any non-Claude backend (codex, grok) is passed
            # through so make_runner can return the matching sibling type.
            runner = (
                _make_runner()
                if resolved_backend == "claude"
                else _make_runner(resolved_backend)
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
            # Readiness self-heal budget: one attempt per issue id per run, and
            # at most `repair_budget` passes overall.
            repair_attempted: set[str] = set()
            repairs_run = 0
            # Console-only dedupe for readiness-skip warnings, keyed on issue
            # id plus summary text so a work spec that re-fails differently warns
            # again. The log keeps every occurrence.
            warned_unready: set[tuple[str, str]] = set()

            while True:
                # Milestone rollover: an epic whose children are all closed
                # is finished work, not a claimable unit — close it here so
                # the next milestone's subtree unblocks and this iteration
                # can claim from it. Must precede the `before` snapshot.
                _rollover_exhausted_epics(bd, write_log)
                before = _snapshot(bd)
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
                        diagnostic = report.diagnostic()
                        write_log(
                            f"readiness skip (left open for planning/human repair): "
                            f"{diagnostic}"
                        )
                        key = (report.issue_id, report.summary())
                        if key in warned_unready:
                            return
                        warned_unready.add(key)
                        output.warn(
                            f"{_unready_skip_line(title, report)}. It stays "
                            "open and unclaimed. Run ortus plan or edit the "
                            "work spec."
                        )

                    if resume_issue_id is not None:
                        # A resume continues an existing claim, whose packet
                        # was frozen at claim time — the machine pipeline
                        # judges that claim-time spec and escalates its
                        # defects. The readiness guard governs FRESH claims
                        # only: routing a resumed claim through it would send
                        # a repair pass against a frozen packet, and the
                        # resulting edit could only trip the acceptance-hash
                        # guard (ortus-qs6l).
                        target_issue = ready_packets[0] if ready_packets else None
                    else:
                        target_issue = select_ready_issue(
                            ready_packets, on_unready=report_unready
                        )

                    # Nothing claimable, but the queue is NOT drained and the
                    # only thing between the loop and real work is work specs that
                    # fail readiness schema v1. Default grind flags those leaves
                    # human and takes the no-ready-issue exit. An explicit
                    # --repair-unready still repairs in place. A queue that also
                    # holds a ready task never reaches here; epics never reach
                    # here either, because select_ready_issue skips them without
                    # reporting them unready. A leftover in_progress resume
                    # never populates ``unready``.
                    repair_blocked: str | None = None
                    if target_issue is None and unready:
                        pending = tuple(
                            report
                            for report in unready
                            if report.issue_id not in repair_attempted
                        )
                        if not repair_unready:
                            _flag_unready_for_human(bd, unready, write_log)
                        elif repairs_run >= repair_budget:
                            repair_blocked = (
                                "readiness repair budget exhausted "
                                f"({repairs_run}/{repair_budget} pass(es) used)"
                            )
                        elif not pending:
                            repair_blocked = (
                                "every unready issue already spent its one repair "
                                "attempt this run"
                            )
                        else:
                            repaired_ids = {report.issue_id for report in pending}
                            repair_attempted |= repaired_ids
                            repairs_run += 1
                            write_log(
                                f"readiness repair pass {repairs_run}/{repair_budget}: "
                                + ", ".join(sorted(repaired_ids))
                            )
                            output.progress(
                                "grind",
                                "repairing unready work spec(s) via one planning pass "
                                "(this typically takes 1-3 min)",
                            )
                            try:
                                repair_rc, repair_summary = _run_readiness_repair(
                                    bd,
                                    pending,
                                    repo=target,
                                    log=log,
                                    write_log=write_log,
                                    backend=resolved_backend,
                                    profile=plan_profile,
                                    probe=codegraph_probe,
                                    timeout=(
                                        worker_timeout if worker_timeout > 0 else None
                                    ),
                                )
                            except RepairCreatedReplacements as exc:
                                # Silent queue growth is the failure mode the
                                # guard exists for: halt instead of skipping.
                                write_log(f"readiness repair: HALT — {exc}")
                                output.error(str(exc))
                                raise typer.Exit(code=1)
                            if repair_rc != 0:
                                repair_blocked = (
                                    f"readiness repair pass failed (exit {repair_rc})"
                                )
                            else:
                                # Reload the authoritative work specs the pass
                                # touched, then re-run the pure selector over
                                # the same queue.
                                reloaded: list[dict] = []
                                for packet in ready_packets:
                                    packet_id = str(packet.get("id") or "").strip()
                                    if packet_id in repaired_ids:
                                        try:
                                            packet = bd.show(packet_id)
                                        except Exception as exc:
                                            write_log(
                                                "readiness repair: could not reload "
                                                f"{packet_id} ({exc})"
                                            )
                                    reloaded.append(packet)
                                ready_packets = reloaded
                                unready.clear()
                                target_issue = select_ready_issue(
                                    ready_packets, on_unready=report_unready
                                )
                                # Record the pass on every issue it touched,
                                # mirroring what the plan verb already does.
                                for repaired_id in sorted(repaired_ids):
                                    try:
                                        bd.add_comment(
                                            repaired_id,
                                            "ortus grind ran one readiness repair "
                                            "pass on this issue.\n\n"
                                            + repair_summary.report(),  # type: ignore[union-attr]
                                        )
                                    except Exception as exc:
                                        write_log(
                                            "readiness repair: could not comment on "
                                            f"{repaired_id} ({exc})"
                                        )
                                if target_issue is None:
                                    repair_blocked = (
                                        "readiness repair left the queue unready"
                                    )
                                else:
                                    write_log(
                                        "readiness repair: "
                                        f"{target_issue['id']} now passes readiness"
                                    )

                    if target_issue is None:
                        # Queue is non-empty (not drained) but nothing is ready —
                        # everything left is blocked or human-flagged. We hold the
                        # flock, so no other actor will unblock it; stop rather
                        # than spin spawning workers that have nothing to do.
                        write_log(
                            "no ready issue to claim (queue blocked or human-only); "
                            f"exiting outer loop. tasks_completed={tasks_completed}"
                        )
                        if repair_blocked is not None:
                            write_log(f"readiness repair: {repair_blocked}")
                            output.error(f"grind: {repair_blocked}")
                        for report in unready:
                            diagnostic = f"readiness: {report.diagnostic()}"
                            follow_up = (
                                f"follow-up: bd update {report.issue_id} "
                                "--description/--design/--acceptance to readiness "
                                f"schema v1, then re-run: ortus grind {target}"
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
                        break
                    issue_id = target_issue["id"]
                    # f2he.2: grind does not claim a fresh ready issue. The
                    # worker claims via goal-prompt. A leftover in_progress
                    # is already claimed; spawn a new process for it.
                    if resume_issue_id is not None:
                        write_log(
                            f"iter prep: continuing leftover claim {issue_id}"
                        )
                    else:
                        write_log(
                            f"iter prep: worker will claim {issue_id} via goal-prompt"
                        )
                    target_issue = bd.show(issue_id)
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
                    try:
                        iteration_prompt = _compose_work_prompt(
                            work_template,
                            target_issue,
                            resolved_backend,
                            phase_instruction=implementation_instruction,
                            phase_contract_text=phase_contract(
                                CodeGraphPhase.IMPLEMENTATION, implementation_probe
                            ),
                            lessons_text=_lessons_contract(bd, write_log),
                        )
                    except BackendError as exc:
                        write_log(f"iter prep: HALT — {exc}")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                    write_log(
                        f"iter {iters_run + 1}: goal-prompt ready for {issue_id} "
                        f"({resolved_backend})"
                    )
                    # output.progress escapes markup itself, so a bracketed
                    # title survives the console without pre-escaping.
                    output.progress(
                        "grind",
                        f'claimed "{target_issue.get("title") or "untitled"}" '
                        f"({issue_id}) — implementing",
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
                implementation_timed_out = False
                # Re-armed every iteration so resume-from-captured can skip a
                # worker spawn; leftover in_progress still runs one.
                implementation_worker_ran = True
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

                try:
                    if implementation_probe.available:
                        write_log("implementation CodeGraph handshake requested")
                    else:
                        output.progress(
                            "grind",
                            "implementation CodeGraph handshake fallback active",
                        )
                    reap_when = None
                    if resolved_backend == "grok":
                        try:
                            baseline_closed = bd.count_by_status("closed")
                        except Exception:
                            baseline_closed = None
                        if baseline_closed is not None:

                            def _reap_on_done_bar() -> bool:
                                label = _done_bar_met(
                                    bd,
                                    git,
                                    baseline_closed,
                                    integration_branch,
                                )
                                if not label:
                                    return False
                                write_log(
                                    f"iter {iters_run}: done bar met "
                                    f"({label}, in sync); "
                                    "reaping grok /goal review"
                                )
                                return True

                            reap_when = _reap_on_done_bar
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
                    implementation_timed_out = True
                    rc = 143  # 128 + SIGTERM; group was SIGTERM'd then SIGKILL'd
                    write_log(
                        f"iter {iters_run}: worker TIMEOUT after {worker_timeout}s, "
                        f"killed (rc={rc})"
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
                    try:
                        judged = bd.show(issue_id)
                    except Exception:
                        judged = {}
                    judged_status = str(judged.get("status") or "open")
                    judged_id = issue_id
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
                    write_log(
                        f"iter {iters_run}: left {judged_id} in_progress "
                        "for the next window"
                    )
                    # One context window per leftover claim. The next grind
                    # invocation is the next window.
                    break
                else:
                    write_log(
                        f"iter {iters_run}: WARN no bd-state change "
                        f"({judged_id} is {judged_status})"
                    )
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
            output.progress(
                "grind",
                f"done — {tasks_completed} landed this session, "
                f"{leftover} in_progress, {final_snapshot.open} open",
            )
            if leftover:
                output.progress(
                    "grind",
                    "next: run `ortus grind` again; it continues leftover "
                    "in_progress",
                )
    except FlockBusy as exc:
        output.error(str(exc), hint="another `ortus grind` is already running here")
        raise typer.Exit(code=1)
