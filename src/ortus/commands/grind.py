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
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import typer

from ortus.core import cache, hooks, output, sandbox
from ortus.core.agent import (
    BackendError,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
)
from ortus.core.bd import BdClient
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
from ortus.core.config import load_config
from ortus.core.profiles import AgentProfile, Phase, ProfileError
from ortus.core.readiness import ReadinessReport
from ortus.core.transaction import CandidateJournal, JournalStore
from ortus.core.transaction import candidate_diff, issue_packet_hash, sha256_bytes
from ortus.core.verdict import (
    VerdictError,
    bound_report,
    parse_verdict,
    render_rejection_report,
    render_report,
)
from ortus.core.git import GitClient
from ortus.core.grind_logic import (
    CONDITION_CEILING,
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
    inject_issue,
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
_EVIDENCE_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)([^\r\n]+)"
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


def _codex_codegraph_handshake(
    runner: ClaudeRunner,
    *,
    repo: Path,
    log_path: Path,
    phase: CodeGraphPhase,
    probe: CodeGraphProbe,
    profile: AgentProfile,
    timeout: float | None,
) -> CodeGraphProbe:
    """Prove child registration in a read-only process before phase work."""
    if not getattr(probe, "available", False):
        return probe
    handshake = getattr(runner, "run_codegraph_handshake", None)
    offset = log_path.stat().st_size if log_path.exists() else 0
    reason: str | None = None
    if not callable(handshake):
        reason = "Codex runner does not support the CodeGraph child handshake"
    else:
        output.progress("grind", f"{phase.value} CodeGraph child handshake probe")
        try:
            rc = handshake(
                phase=phase.value,
                repo=repo,
                log_path=log_path,
                profile=profile,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            rc = 143
            reason = "Codex CodeGraph child handshake timed out"
        except OSError as exc:
            rc = 1
            reason = f"Codex CodeGraph child handshake could not launch: {exc}"
        summary = parse_transcript(
            log_path, phase=phase, probe=probe, start_offset=offset
        )
        append_normalized(log_path, summary)
        if rc != 0 and reason is None:
            reason = f"Codex CodeGraph child handshake exited {rc}"
        elif not summary.capability_observed:
            reason = "; ".join(summary.fallbacks[:3])
        else:
            output.progress(
                "grind", f"{phase.value} CodeGraph child handshake succeeded"
            )
            _append_handshake(log_path, phase, success=True)
            return probe

    assert reason is not None
    _append_handshake(log_path, phase, success=False, reason=reason)
    if probe.mode is CodeGraphMode.REQUIRED:
        raise CodeGraphUnavailable(f"CodeGraph required but {reason}")
    output.progress("grind", f"{phase.value} CodeGraph fallback: {reason}")
    return replace(probe, available=False, reason=reason, capability=None)


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


def _append_verdict_event(
    log_path: Path, *, decision: str, candidate_hash: str, reason: str = ""
) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "ortus.verdict",
                    "schema": 1,
                    "decision": decision,
                    "candidate_hash": candidate_hash,
                    "reason": reason,
                },
                separators=(",", ":"),
            )
            + "\n"
        )


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
            hint="inspect the staged tracker exports and git configuration",
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
            "preflight: preserving dirty worktree for worker handoff: "
            + ", ".join(sorted(remaining))
        )
    return remaining


def _capture_codex_candidate(
    git: GitClient,
    store: JournalStore,
    journal: CandidateJournal,
    baseline: frozenset[str],
    *,
    phase: str,
) -> CandidateJournal:
    """Persist current candidate ownership without absorbing baseline edits."""

    dirty = git.dirty_paths()
    if dirty is None:
        output.error("grind: could not capture Codex candidate paths")
        raise typer.Exit(code=1)
    paths = dirty - baseline - _TRACKER_EXPORT_PATHS
    try:
        diff = candidate_diff(git.repo, paths)
    except RuntimeError as exc:
        output.error(f"grind: could not create candidate diff: {exc}")
        raise typer.Exit(code=1) from exc
    digest, diff_ref = store.save_diff(diff)
    updated = journal.with_candidate(
        paths, phase=phase, candidate_hash=digest, diff_ref=diff_ref
    )
    store.save(updated)
    return updated


def _packet_artifact_intact(repo: Path, journal: CandidateJournal) -> bool:
    """Rehash the on-disk issue packet the verifier is about to be handed.

    The packet reference is a plain file under ``logs/``, so a worker that
    ignores the phase contract could rewrite it and verify itself against a
    packet nobody authorized. bd being unchanged is not enough — the artifact
    bytes must still hash to the advertised digest.
    """

    if not journal.issue_packet_ref or not journal.issue_packet_hash:
        return True
    try:
        payload = (repo / journal.issue_packet_ref).read_bytes()
    except OSError:
        return False
    return sha256_bytes(payload) == journal.issue_packet_hash


def _capture_evidence(
    store: JournalStore,
    journal: CandidateJournal,
    log_path: Path,
    *,
    start_offset: int,
    returncode: int,
    timed_out: bool,
) -> CandidateJournal:
    """Attach bounded, durable implementation evidence to the transaction."""

    try:
        with log_path.open("rb") as fh:
            fh.seek(start_offset)
            transcript = fh.read(64_001)
    except OSError:
        transcript = b""
    truncated = len(transcript) > 64_000
    transcript = transcript[:64_000]
    excerpt = transcript.decode("utf-8", errors="replace")
    excerpt = _EVIDENCE_SECRET.sub(r"\1\2[REDACTED]", excerpt)
    item = {
        "kind": "implementation-transcript",
        "returncode": returncode,
        "timed_out": timed_out,
        "sha256": sha256_bytes(transcript),
        "excerpt": excerpt,
        "truncated": truncated,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    updated = journal.with_evidence(item)
    store.save(updated)
    return updated


def _verifier_prompt(journal: CandidateJournal, probe_text: str) -> str:
    """Compose a bounded verifier contract referencing immutable artifacts."""

    return (
        "FRESH READ-ONLY VERIFICATION PHASE. You cannot edit source or bd state. "
        "Independently inspect the exact issue packet and candidate diff at the paths "
        "below, run bounded read-only checks (disable pytest cache writes), and emit "
        "exactly one final assistant line beginning ORTUS_VERDICT: followed by one JSON "
        "object. Do not emit that prefix anywhere else.\n\n"
        f"Issue: {journal.issue_id}\n"
        f"Issue packet: {journal.issue_packet_ref}\n"
        f"Issue packet SHA-256: {journal.issue_packet_hash}\n"
        f"Candidate diff: {journal.candidate_diff_ref}\n"
        f"Candidate SHA-256: {journal.candidate_hash}\n"
        f"Captured evidence: {json.dumps(journal.evidence, ensure_ascii=False)}\n\n"
        "The JSON object must have exactly these fields: schema (1), candidate_hash, "
        "decision (pass or fail), criteria (non-empty array of objects with exactly id, "
        "status, evidence), and non-empty string arrays commands, reviewed_files, "
        "reviewed_interfaces, risks, findings, codegraph. A pass requires every criterion "
        "to pass; a fail requires at least one failed criterion. Bind candidate_hash to "
        "the supplied SHA-256 exactly.\n" + probe_text
    )


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
    """Ground a grind-side repair in each packet plus its parent epic.

    A grind run has no PRD, and guessing one would repair the packet against
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
        "each packet from its own `bd show <id> --json` output and the parent "
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
    instead of updating the packets it was named — that is a hard error, not a
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
            if not epic_is_exhausted(full):
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
        "stop. Otherwise complete only this issue, leave the result as uncommitted "
        "candidate edits, then end the session."
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
            "bd/dolt/build processes and releasing their locks). Codex preserves the "
            "claimed candidate for restart; Claude runs bd-state/orphan-policy "
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
        True,
        "--repair-unready/--no-repair-unready",
        help=(
            "When the ready queue holds only leaves that fail readiness schema "
            "v1, repair them in place with one planning-profile pass and keep "
            "going. --no-repair-unready restores the skip-and-stop behavior."
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
        config = load_config(repo=target)
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
        # Repairing an unready packet is authoring work, not implementation, so
        # the self-heal pass runs on the planning profile.
        plan_profile = config.resolve_profile(resolved_backend, Phase.PLAN)
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
        output.info(
            "repair:         "
            + (
                f"{plan_profile.display_name}, budget {repair_budget} pass(es)"
                if repair_unready and repair_budget > 0
                else "off (unready leaves are skipped)"
            )
        )
        output.info(f"codegraph:      {codegraph_mode.value}")
        output.info(
            f"select:         {'harness (per-iteration claim)' if harness_select else 'worker (legacy --condition)'}"
        )
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
            write_log(f"phase profile: {implement_profile.display_name}")
            write_log(f"phase profile: {verify_profile.display_name}")
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
            transaction_store = JournalStore(target)
            active_journal: CandidateJournal | None = None
            codex_baseline = frozenset[str]()
            startup_handoff_paths = frozenset[str]()
            resume_issue_id: str | None = None
            resume_candidate_ready = False
            recovery_handoff = False
            if resolved_backend == "codex":
                active_journal = transaction_store.load()
                if active_journal is None and transaction_store.path.exists():
                    dirty = git.dirty_paths()
                    claimed = bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS)
                    if dirty is not None and len(claimed) == 1:
                        issue_hint = next(iter(claimed))
                        handoff_paths = dirty - _TRACKER_EXPORT_PATHS
                        handoff_diff = candidate_diff(target, handoff_paths)
                        handoff_hash, handoff_ref = transaction_store.save_diff(
                            handoff_diff
                        )
                        active_journal = CandidateJournal.start(
                            repo=target,
                            issue_id=issue_hint,
                            base_head=git.head_oid(),
                            baseline_paths=(),
                        ).with_candidate(
                            handoff_paths,
                            phase="handoff",
                            candidate_hash=handoff_hash,
                            diff_ref=handoff_ref,
                        )
                        transaction_store.save(active_journal)
                        write_log(
                            "transaction handoff: rebuilt unusable journal from "
                            f"claimed issue {issue_hint} and current worktree"
                        )
                if active_journal is None:
                    if transaction_store.path.exists():
                        write_log(
                            "transaction handoff: journal is unusable and no single "
                            "claimed issue identifies the work; continuing with the "
                            "current worktree as worker context"
                        )
                    startup_changes = _checkpoint_codex_preflight(
                        git,
                        integration_branch,
                        write_log,
                        accept_baseline=True,
                    )
                    if startup_changes:
                        # Existing work is an engineering handoff, not a reason
                        # to fence the worker away from potentially useful code.
                        # The worker receives the complete diff and decides what
                        # advances the selected issue.
                        codex_baseline = frozenset()
                        startup_handoff_paths = startup_changes
                        recovery_handoff = True
                        write_log(
                            "transaction handoff: presenting existing uncommitted "
                            "changes to the next worker for relevance assessment"
                        )
                else:
                    recovery_handoff = True
                    resumed_phase = active_journal.phase
                    dirty = git.dirty_paths()
                    current_head = git.head_oid()
                    if dirty is None:
                        output.error(
                            "grind: could not read the worktree for recovery handoff"
                        )
                        raise typer.Exit(code=1)
                    baseline_changed = not active_journal.baseline_is_unchanged(target)
                    if current_head != active_journal.base_head or baseline_changed:
                        write_log(
                            "transaction handoff: repository state moved since the "
                            "prior worker; adopting the current worktree for model review"
                        )
                    active_journal = replace(
                        active_journal,
                        base_head=current_head,
                        baseline_paths=(),
                        baseline_fingerprints={},
                    )
                    codex_baseline = frozenset()
                    current_candidate = dirty - codex_baseline - _TRACKER_EXPORT_PATHS
                    recorded_candidate = frozenset(active_journal.candidate_paths)
                    sealed_phases = {
                        "implementation-timeout",
                        "verification-timeout",
                        "orphaned-candidate",
                        "incomplete-candidate",
                    }
                    if (
                        active_journal.phase in sealed_phases
                        and current_candidate != recorded_candidate
                    ):
                        write_log(
                            "transaction handoff: candidate path set changed since "
                            "the prior worker; the next worker will assess the current diff"
                        )
                    current_diff = candidate_diff(target, current_candidate)
                    current_hash = sha256_bytes(current_diff)
                    if active_journal.candidate_hash != current_hash:
                        migrated_hash, migrated_ref = transaction_store.save_diff(
                            current_diff
                        )
                        active_journal = active_journal.with_candidate(
                            current_candidate,
                            phase=resumed_phase,
                            candidate_hash=migrated_hash,
                            diff_ref=migrated_ref,
                        )
                        transaction_store.save(active_journal)
                        write_log(
                            "transaction handoff: refreshed candidate for model review "
                            f"hash={migrated_hash}"
                        )
                    resume_candidate_ready = resumed_phase in {
                        "candidate-captured",
                        "verification",
                        "verification-timeout",
                    }
                    active_journal = active_journal.with_candidate(
                        current_candidate, phase=resumed_phase
                    )
                    transaction_store.save(active_journal)
                    resume_issue_id = active_journal.issue_id
                    _checkpoint_codex_preflight(
                        git,
                        integration_branch,
                        write_log,
                        allowed_dirty=dirty,
                        checkpoint_tracker=False,
                    )
                    write_log(
                        f"transaction resume: issue={resume_issue_id} "
                        f"candidate_paths={sorted(current_candidate)}"
                    )
                    output.progress(
                        "grind", f"resuming preserved Codex candidate {resume_issue_id}"
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
            orphan_ids = initial_snapshot.in_progress_ids - (
                {resume_issue_id} if resume_issue_id is not None else set()
            )
            if orphan_ids:
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
                _make_runner()
                if resolved_backend == "claude"
                else _make_runner("codex")
            )
            configure_codegraph = getattr(runner, "configure_codegraph", None)
            if callable(configure_codegraph):
                configure_codegraph(codegraph_probe.capability)
            runner.extra_env.update(cache_env)

            tasks_completed = 0
            iters_run = 0
            # Readiness self-heal budget: one attempt per issue id per run, and
            # at most `repair_budget` passes overall.
            repair_attempted: set[str] = set()
            repairs_run = 0

            while True:
                # Milestone rollover: an epic whose children are all closed
                # is finished work, not a claimable unit — close it here so
                # the next milestone's subtree unblocks and this iteration
                # can claim from it. Must precede the `before` snapshot.
                _rollover_exhausted_epics(bd, write_log)
                before = _snapshot(bd)
                implementation_probe = codegraph_probe
                verification_probe = codegraph_probe
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

                # Queue reads can auto-export generated Beads state between
                # iterations. Checkpoint that state while preserving the dirty
                # current handoff and any active candidate context.
                if resolved_backend == "codex":
                    allowed = codex_baseline | startup_handoff_paths
                    if active_journal is not None:
                        allowed |= frozenset(active_journal.candidate_paths)
                        allowed |= _TRACKER_EXPORT_PATHS
                    _checkpoint_codex_preflight(
                        git,
                        integration_branch,
                        write_log,
                        allowed_dirty=allowed,
                        checkpoint_tracker=active_journal is None,
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
                    # authoritative packet before the readiness guard decides
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
                                f"issue packet ({exc})"
                            )
                            write_log(message)
                            output.warn(message)

                    unready: list[ReadinessReport] = []

                    def report_unready(
                        candidate: dict, report: ReadinessReport
                    ) -> None:
                        unready.append(report)
                        diagnostic = report.diagnostic()
                        message = (
                            f"readiness skip (left open for planning/human repair): "
                            f"{diagnostic}"
                        )
                        write_log(message)
                        output.warn(message)

                    target_issue = select_ready_issue(
                        ready_packets, on_unready=report_unready
                    )

                    # Nothing claimable, but the queue is NOT drained and the
                    # only thing between the loop and real work is packets that
                    # fail readiness schema v1. Repair those in place and
                    # revalidate rather than dead-ending on an authoring defect
                    # (ortus-xhrj.7). A queue that also holds a ready leaf never
                    # reaches here, so it spends no repair budget; epics never
                    # reach here either, because select_ready_issue skips them
                    # without reporting them unready.
                    repair_blocked: str | None = None
                    if target_issue is None and unready:
                        pending = tuple(
                            report
                            for report in unready
                            if report.issue_id not in repair_attempted
                        )
                        if not repair_unready:
                            repair_blocked = (
                                "readiness repair disabled by --no-repair-unready"
                            )
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
                                "repairing unready packet(s) via one planning pass "
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
                                # Reload the authoritative packets the pass
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
                            output.error(diagnostic)
                            output.error(follow_up)
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
                    # Freeze the post-claim authoritative packet. Verification
                    # is bound to this exact JSON identity, including status.
                    target_issue = bd.show(issue_id)
                    if resolved_backend == "codex":
                        phase_profiles = {
                            "implementation": implement_profile.display_name,
                            "verification": verify_profile.display_name,
                        }
                        packet_digest, packet_ref = transaction_store.save_packet(
                            issue_id, target_issue
                        )
                        if active_journal is None:
                            active_journal = CandidateJournal.start(
                                repo=target,
                                issue_id=issue_id,
                                base_head=git.head_oid(),
                                baseline_paths=codex_baseline,
                                packet_hash=packet_digest,
                                packet_ref=packet_ref,
                                profiles=phase_profiles,
                            )
                        elif not active_journal.issue_packet_hash:
                            active_journal = replace(
                                active_journal,
                                issue_packet_hash=packet_digest,
                                issue_packet_ref=packet_ref,
                            )
                            transaction_store.save(active_journal)
                            write_log(
                                "transaction migration: bound schema-v1 candidate "
                                f"to issue packet {packet_digest}"
                            )
                        elif (
                            active_journal.issue_packet_hash != packet_digest
                            or active_journal.issue_packet_ref != packet_ref
                        ):
                            write_log(
                                "transaction handoff: issue packet changed since the "
                                "prior worker; adopting the current authoritative packet"
                            )
                            active_journal = replace(
                                active_journal,
                                issue_packet_hash=packet_digest,
                                issue_packet_ref=packet_ref,
                            )
                            transaction_store.save(active_journal)
                        if active_journal.profiles != phase_profiles:
                            active_journal = replace(
                                active_journal, profiles=phase_profiles
                            )
                            transaction_store.save(active_journal)
                        dirty_after_claim = git.dirty_paths()
                        if dirty_after_claim is None:
                            output.error(
                                "grind: could not record Codex candidate ownership"
                            )
                            raise typer.Exit(code=1)
                        active_journal = active_journal.with_candidate(
                            dirty_after_claim - codex_baseline - _TRACKER_EXPORT_PATHS,
                            phase="implementation",
                        )
                        transaction_store.save(active_journal)
                        resume_issue_id = None
                    else:
                        packet_digest, packet_ref = transaction_store.save_packet(
                            issue_id, target_issue
                        )
                        active_journal = CandidateJournal.start(
                            repo=target,
                            issue_id=issue_id,
                            base_head=git.head_oid(),
                            baseline_paths=(),
                            packet_hash=packet_digest,
                            packet_ref=packet_ref,
                            profiles={
                                "implementation": implement_profile.display_name,
                                "verification": verify_profile.display_name,
                            },
                        )
                        transaction_store.save(active_journal)
                    configure_codegraph = getattr(runner, "configure_codegraph", None)
                    if callable(configure_codegraph):
                        configure_codegraph(codegraph_probe.capability)
                    try:
                        implementation_probe = (
                            _codex_codegraph_handshake(
                                runner,
                                repo=target,
                                log_path=log,
                                phase=CodeGraphPhase.IMPLEMENTATION,
                                probe=codegraph_probe,
                                profile=implement_profile,
                                timeout=(
                                    worker_timeout if worker_timeout > 0 else None
                                ),
                            )
                            if resolved_backend == "codex"
                            else codegraph_probe
                        )
                    except CodeGraphUnavailable as exc:
                        bd.update_status(issue_id, "open")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                    if callable(configure_codegraph):
                        configure_codegraph(implementation_probe.capability)
                    implementation_instruction = (
                        "IMPLEMENTATION PHASE ONLY. These phase rules override any "
                        "conflicting instruction elsewhere in this prompt. Make candidate "
                        "edits and run targeted checks, but do not close the issue, do not "
                        "run git commit or git push, do not select other work, and do not "
                        "add the final verification comment; a fresh read-only verifier "
                        "reviews your exact candidate next and Ortus owns finalization. "
                        "HEAD must be unchanged when you finish — a commit invalidates the "
                        "candidate."
                    )
                    if recovery_handoff and not resume_candidate_ready:
                        implementation_instruction += (
                            " RECOVERY HANDOFF: A prior engineer left uncommitted work. "
                            "Inspect `git status`, the complete current diff, and the issue "
                            "packet. Decide which existing changes advance this issue, keep "
                            "useful work, correct or complete it, and leave unrelated changes "
                            "untouched. Continue from the current state instead of restarting."
                        )
                    try:
                        iteration_prompt = _compose_work_prompt(
                            work_template,
                            target_issue,
                            resolved_backend,
                            phase_instruction=implementation_instruction,
                            phase_contract_text=phase_contract(
                                CodeGraphPhase.IMPLEMENTATION, implementation_probe
                            ),
                        )
                    except BackendError as exc:
                        # Revert the claim before halting: a worker was never
                        # spawned, so leaving the issue in_progress would
                        # strand it.
                        bd.update_status(issue_id, "open")
                        write_log(f"iter prep: HALT — {exc}")
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
                implementation_timed_out = False
                phase_offset = log.stat().st_size if log.exists() else 0
                try:
                    output.progress(
                        "grind",
                        "implementation CodeGraph handshake "
                        + (
                            "requested"
                            if implementation_probe.available
                            else "fallback active"
                        ),
                    )
                    if resume_candidate_ready:
                        rc = int(
                            active_journal.evidence[-1].get("returncode", 0)
                            if active_journal and active_journal.evidence
                            else 0
                        )
                        write_log(
                            f"iter {iters_run}: implementation already captured; "
                            "resuming at verification"
                        )
                    else:
                        rc = runner.run(
                            iteration_prompt,
                            repo=target,
                            log_path=log,
                            fast=fast,
                            profile=implement_profile,
                            timeout=(worker_timeout if worker_timeout > 0 else None),
                        )
                except subprocess.TimeoutExpired:
                    implementation_timed_out = True
                    rc = 143  # 128 + SIGTERM; group was SIGTERM'd then SIGKILL'd
                    write_log(
                        f"iter {iters_run}: worker TIMEOUT after {worker_timeout}s, "
                        f"killed (rc={rc})"
                    )

                if (
                    resolved_backend == "claude"
                    and implementation_timed_out
                    and active_journal is not None
                    and issue_id in _snapshot(bd).in_progress_ids
                ):
                    if git.is_git_repo():
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            frozenset(),
                            phase="implementation-timeout",
                        )
                    active_journal = _capture_evidence(
                        transaction_store,
                        active_journal,
                        log,
                        start_offset=phase_offset,
                        returncode=rc,
                        timed_out=True,
                    )
                    action = apply_orphan_policy(
                        orphan_policy,
                        {issue_id},
                        revert_fn=lambda i: bd.update_status(i, "open"),
                        escalate_fn=lambda i: bd.add_label(i, "human"),
                    )
                    for line in action.actions_taken:
                        write_log(f"  orphan-policy: {line}")
                    break

                if (
                    resolved_backend == "codex"
                    and implementation_timed_out
                    and active_journal is not None
                ):
                    active_journal = _capture_codex_candidate(
                        git,
                        transaction_store,
                        active_journal,
                        codex_baseline,
                        phase="implementation-timeout",
                    )
                    active_journal = _capture_evidence(
                        transaction_store,
                        active_journal,
                        log,
                        start_offset=phase_offset,
                        returncode=rc,
                        timed_out=True,
                    )
                    active_journal = active_journal.with_candidate(
                        active_journal.candidate_paths,
                        phase="implementation-timeout",
                    )
                    transaction_store.save(active_journal)
                    write_log(
                        f"iter {iters_run}: preserved timed-out candidate for "
                        f"{active_journal.issue_id}: "
                        f"{list(active_journal.candidate_paths)}"
                    )
                    output.progress(
                        "grind",
                        f"preserved timed-out candidate {active_journal.issue_id}; "
                        "re-run grind to resume",
                    )
                    break

                if active_journal is not None and not resume_candidate_ready:
                    if git.is_git_repo():
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            codex_baseline,
                            phase="candidate-captured",
                        )
                    else:
                        digest, diff_ref = transaction_store.save_diff(b"")
                        active_journal = active_journal.with_candidate(
                            (),
                            phase="candidate-captured",
                            candidate_hash=digest,
                            diff_ref=diff_ref,
                        )
                        transaction_store.save(active_journal)
                    active_journal = _capture_evidence(
                        transaction_store,
                        active_journal,
                        log,
                        start_offset=phase_offset,
                        returncode=rc,
                        timed_out=implementation_timed_out,
                    )

                if resolved_backend == "claude":
                    rejection = _claude_goal_rejection(log, start_offset=phase_offset)
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
                    probe=implementation_probe,
                    start_offset=phase_offset,
                )
                append_normalized(log, implementation_summary)
                if implementation_summary.capability_observed:
                    output.progress(
                        "grind", "implementation CodeGraph handshake succeeded"
                    )
                elif codegraph_mode is not CodeGraphMode.OFF:
                    output.progress(
                        "grind",
                        "implementation CodeGraph fallback: "
                        + "; ".join(implementation_summary.fallbacks[:3]),
                    )
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

                if (
                    harness_select
                    and active_journal is not None
                    and not implementation_timed_out
                ):
                    implementation_packet = bd.show(issue_id)
                    status_changed = (
                        implementation_packet.get("status") != "in_progress"
                    )
                    # Ordered most-specific-first: whichever isolation boundary
                    # broke, the operator sees the concrete one in the report.
                    reason = None
                    if status_changed:
                        reason = "implementation worker changed issue lifecycle state"
                    elif (
                        issue_packet_hash(implementation_packet)
                        != active_journal.issue_packet_hash
                    ):
                        reason = (
                            "implementation worker changed the immutable issue packet"
                        )
                    elif not _packet_artifact_intact(target, active_journal):
                        reason = (
                            "implementation worker changed the immutable issue packet "
                            "artifact"
                        )
                    elif (
                        git.is_git_repo()
                        and git.head_oid() != active_journal.base_head
                    ):
                        # A commit moves HEAD, so `git diff HEAD` would report an
                        # empty candidate and the verifier would review nothing.
                        reason = (
                            "implementation worker committed to the repository; the "
                            f"candidate base {active_journal.base_head} is no longer HEAD"
                        )
                    if reason is not None:
                        if status_changed:
                            bd.update_status(issue_id, "in_progress")
                        report = (
                            "## Ortus implementation isolation report\n\n"
                            f"Issue: {issue_id}\n"
                            f"Candidate: `{active_journal.candidate_hash}`\n"
                            "Decision: **REJECTED**\n\n"
                            f"Finding: {reason}.\n\n" + implementation_summary.report()
                        )
                        report_ref = transaction_store.save_report(
                            active_journal.candidate_hash,
                            report,
                            attempt=active_journal.attempt,
                        )
                        bd.add_comment(issue_id, report)
                        active_journal = active_journal.finish_verification(
                            report_ref, phase="implementation-rejected"
                        )
                        transaction_store.save(active_journal)
                        write_log(f"iter {iters_run}: {reason}; candidate rejected")
                        output.error(f"grind: {reason}; candidate rejected")
                        break

                # Candidate edits are indexed by the parent before a fresh
                # verifier starts. Refresh failure is blocking only in required
                # mode (auto records the stale fallback and continues).
                output.progress(
                    "grind", "refreshing CodeGraph index before verification"
                )
                freshness, sync_ms = codegraph_adapter.refresh(target, codegraph_probe)
                implementation_summary.freshness = freshness
                implementation_summary.sync_duration_ms = sync_ms
                write_log(
                    f"CodeGraph refresh: result={freshness} duration_ms={sync_ms}"
                )
                if (
                    freshness == "sync-failed"
                    and codegraph_mode is CodeGraphMode.REQUIRED
                ):
                    if harness_select:
                        bd.update_status(issue_id, "open")
                    output.error(
                        "CodeGraph required but index refresh failed before verification"
                    )
                    raise typer.Exit(code=1)

                mid = _snapshot(bd)
                if harness_select and issue_id in mid.in_progress_ids:
                    if active_journal is None:
                        output.error("grind: verifier has no candidate transaction")
                        raise typer.Exit(code=1)
                    active_journal = active_journal.begin_verification()
                    transaction_store.save(active_journal)
                    if callable(configure_codegraph):
                        configure_codegraph(codegraph_probe.capability)
                    try:
                        verification_probe = (
                            _codex_codegraph_handshake(
                                runner,
                                repo=target,
                                log_path=log,
                                phase=CodeGraphPhase.VERIFICATION,
                                probe=codegraph_probe,
                                profile=verify_profile,
                                timeout=(
                                    worker_timeout if worker_timeout > 0 else None
                                ),
                            )
                            if resolved_backend == "codex"
                            else codegraph_probe
                        )
                    except CodeGraphUnavailable as exc:
                        bd.update_status(issue_id, "open")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                    if callable(configure_codegraph):
                        configure_codegraph(verification_probe.capability)
                    verifier_prompt = _verifier_prompt(
                        active_journal,
                        phase_contract(CodeGraphPhase.VERIFICATION, verification_probe),
                    )
                    verify_offset = log.stat().st_size if log.exists() else 0
                    output.progress(
                        "grind",
                        "verification CodeGraph handshake "
                        + (
                            "requested"
                            if codegraph_probe.available
                            else "fallback active"
                        ),
                    )
                    verification_timed_out = False
                    try:
                        rc = runner.run(
                            verifier_prompt,
                            repo=target,
                            log_path=log,
                            fast=False,
                            profile=verify_profile,
                            timeout=(worker_timeout if worker_timeout > 0 else None),
                            readonly=True,
                        )
                    except subprocess.TimeoutExpired:
                        verification_timed_out = True
                        rc = 143
                        write_log(
                            f"iter {iters_run}: verifier TIMEOUT after {worker_timeout}s"
                        )
                    if verification_timed_out and active_journal is not None:
                        timeout_failure = (
                            f"verifier timed out after {worker_timeout}s without a verdict"
                        )
                        timeout_phase = "verification-timeout"
                        if git.is_git_repo():
                            post_dirty = git.dirty_paths()
                            if post_dirty is None:
                                timeout_failure += (
                                    "; could not inspect the candidate after timeout"
                                )
                                timeout_phase = "verification-rejected"
                            else:
                                post_paths = (
                                    post_dirty - codex_baseline - _TRACKER_EXPORT_PATHS
                                )
                                if post_paths != frozenset(
                                    active_journal.candidate_paths
                                ):
                                    timeout_failure += (
                                        "; verifier mutated the candidate path set"
                                    )
                                    timeout_phase = "verification-rejected"
                                else:
                                    try:
                                        post_diff = candidate_diff(target, post_paths)
                                    except RuntimeError as exc:
                                        timeout_failure += (
                                            f"; could not hash the candidate: {exc}"
                                        )
                                        timeout_phase = "verification-rejected"
                                    else:
                                        if (
                                            sha256_bytes(post_diff)
                                            != active_journal.candidate_hash
                                        ):
                                            timeout_failure += (
                                                "; verifier mutated the candidate"
                                            )
                                            timeout_phase = "verification-rejected"
                        current_packet = bd.show(issue_id)
                        if (
                            issue_packet_hash(current_packet)
                            != active_journal.issue_packet_hash
                        ):
                            timeout_failure += (
                                "; authoritative issue packet changed during verification"
                            )
                            timeout_phase = "verification-rejected"
                            if current_packet.get("status") != "in_progress":
                                bd.update_status(issue_id, "in_progress")
                        verification_summary = parse_transcript(
                            log,
                            phase=CodeGraphPhase.VERIFICATION,
                            probe=verification_probe,
                            start_offset=verify_offset,
                        )
                        verification_summary.freshness = freshness
                        verification_summary.sync_duration_ms = sync_ms
                        append_normalized(log, verification_summary)
                        report = render_rejection_report(
                            issue_id=issue_id,
                            candidate_hash=active_journal.candidate_hash,
                            failure=timeout_failure,
                            expected_criteria=dict.fromkeys(
                                re.findall(
                                    r"\bAC-\d+\b",
                                    str(target_issue.get("acceptance_criteria", "")),
                                )
                            ),
                            base_head=active_journal.base_head,
                            issue_packet_hash=active_journal.issue_packet_hash,
                            attempt=active_journal.attempt,
                            profiles=active_journal.profiles,
                        )
                        report = bound_report(
                            report + "\n" + verification_summary.report()
                        )
                        report_ref = transaction_store.save_report(
                            active_journal.candidate_hash,
                            report,
                            attempt=active_journal.attempt,
                        )
                        bd.add_comment(issue_id, report)
                        active_journal = active_journal.finish_verification(
                            report_ref, phase=timeout_phase
                        )
                        transaction_store.save(active_journal)
                        _append_verdict_event(
                            log,
                            decision="rejected",
                            candidate_hash=active_journal.candidate_hash,
                            reason=timeout_failure,
                        )
                        write_log(
                            f"iter {iters_run}: preserved verifier-timeout candidate "
                            f"for {active_journal.issue_id}: "
                            f"{list(active_journal.candidate_paths)}"
                        )
                        output.progress(
                            "grind",
                            f"preserved timed-out candidate {active_journal.issue_id}; "
                            "re-run grind to resume",
                        )
                        break
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
                        probe=verification_probe,
                        start_offset=verify_offset,
                    )
                    verification_summary.freshness = freshness
                    verification_summary.sync_duration_ms = sync_ms
                    append_normalized(log, verification_summary)
                    if verification_summary.capability_observed:
                        output.progress(
                            "grind", "verification CodeGraph handshake succeeded"
                        )
                    elif codegraph_mode is not CodeGraphMode.OFF:
                        output.progress(
                            "grind",
                            "verification CodeGraph fallback: "
                            + "; ".join(verification_summary.fallbacks[:3]),
                        )
                    try:
                        require_handshake(verification_summary)
                    except CodeGraphUnavailable as exc:
                        bd.update_status(issue_id, "open")
                        output.error(str(exc))
                        raise typer.Exit(code=1)
                    failure: str | None = None
                    verdict = None
                    try:
                        verdict = parse_verdict(
                            log,
                            start_offset=verify_offset,
                            expected_hash=active_journal.candidate_hash,
                            expected_criteria=dict.fromkeys(
                                re.findall(
                                    r"\bAC-\d+\b",
                                    str(target_issue.get("acceptance_criteria", "")),
                                )
                            ),
                        )
                        if git.is_git_repo():
                            post_dirty = git.dirty_paths()
                            if post_dirty is None:
                                raise VerdictError(
                                    "could not inspect the candidate after verification"
                                )
                            post_paths = (
                                post_dirty - codex_baseline - _TRACKER_EXPORT_PATHS
                            )
                            if post_paths != frozenset(active_journal.candidate_paths):
                                raise VerdictError(
                                    "verifier mutated the candidate path set during "
                                    "read-only review"
                                )
                            post_diff = candidate_diff(target, post_paths)
                            if sha256_bytes(post_diff) != active_journal.candidate_hash:
                                raise VerdictError(
                                    "verifier mutated the candidate during read-only review"
                                )
                        current_packet = bd.show(issue_id)
                        if (
                            issue_packet_hash(current_packet)
                            != active_journal.issue_packet_hash
                        ):
                            raise VerdictError(
                                "authoritative issue packet changed during verification"
                            )
                    except (VerdictError, RuntimeError) as exc:
                        failure = str(exc)
                    if rc != 0 and failure is None:
                        failure = f"verifier exited with status {rc}"

                    if verdict is not None and failure is None:
                        report = render_report(
                            verdict,
                            issue_id=issue_id,
                            base_head=active_journal.base_head,
                            issue_packet_hash=active_journal.issue_packet_hash,
                            attempt=active_journal.attempt,
                            profiles=active_journal.profiles,
                        )
                        report = bound_report(
                            report + "\n" + verification_summary.report()
                        )
                        report_ref = transaction_store.save_report(
                            active_journal.candidate_hash,
                            report,
                            attempt=active_journal.attempt,
                        )
                        bd.add_comment(issue_id, report)
                        active_journal = active_journal.finish_verification(
                            report_ref,
                            phase=(
                                "verified-pass" if verdict.passed else "verified-fail"
                            ),
                        )
                        transaction_store.save(active_journal)
                        _append_verdict_event(
                            log,
                            decision=verdict.decision,
                            candidate_hash=active_journal.candidate_hash,
                        )
                        write_log(
                            f"iter {iters_run}: verifier verdict={verdict.decision} "
                            f"candidate={active_journal.candidate_hash}"
                        )
                    else:
                        assert failure is not None
                        # A verifier is observational only. A fake/misconfigured
                        # runner that changed lifecycle state cannot make its
                        # own output authoritative; restore the claim before
                        # persisting the rejection.
                        if bd.show(issue_id).get("status") != "in_progress":
                            bd.update_status(issue_id, "in_progress")
                        report = render_rejection_report(
                            issue_id=issue_id,
                            candidate_hash=active_journal.candidate_hash,
                            failure=failure,
                            expected_criteria=dict.fromkeys(
                                re.findall(
                                    r"\bAC-\d+\b",
                                    str(target_issue.get("acceptance_criteria", "")),
                                )
                            ),
                            base_head=active_journal.base_head,
                            issue_packet_hash=active_journal.issue_packet_hash,
                            attempt=active_journal.attempt,
                            profiles=active_journal.profiles,
                        )
                        report = bound_report(
                            report + "\n" + verification_summary.report()
                        )
                        report_ref = transaction_store.save_report(
                            active_journal.candidate_hash,
                            report,
                            attempt=active_journal.attempt,
                        )
                        bd.add_comment(issue_id, report)
                        active_journal = active_journal.finish_verification(
                            report_ref, phase="verification-rejected"
                        )
                        transaction_store.save(active_journal)
                        _append_verdict_event(
                            log,
                            decision="rejected",
                            candidate_hash=active_journal.candidate_hash,
                            reason=failure,
                        )
                        write_log(f"iter {iters_run}: verifier rejected: {failure}")
                        output.error(f"grind: verifier rejected candidate: {failure}")
                        break
                else:
                    # Compatibility/safety: if an implementation worker closed
                    # despite the phase contract, still leave durable evidence.
                    verification_summary = implementation_summary
                    verification_summary.phase = CodeGraphPhase.VERIFICATION.value

                output.progress(
                    "grind",
                    f"CodeGraph phase summary: {len(verification_summary.events)} queries, "
                    f"freshness={freshness}",
                )

                after = _snapshot(bd)
                delta = compute_delta(before, after)

                if (
                    active_journal is not None
                    and active_journal.phase in {"verified-pass", "verified-fail"}
                    and active_journal.issue_id in after.in_progress_ids
                ):
                    write_log(
                        f"iter {iters_run}: candidate transaction ended in "
                        f"{active_journal.phase}; finalization is a later lifecycle step"
                    )
                    output.progress(
                        "grind",
                        f"candidate {active_journal.candidate_hash} "
                        f"{active_journal.phase}; awaiting finalization",
                    )
                    break

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
                        if active_journal is None:
                            output.error(
                                "grind: closed Codex issue has no ownership journal"
                            )
                            raise typer.Exit(code=1)
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            codex_baseline,
                            phase="finalizing",
                        )
                        owned_paths = frozenset(active_journal.candidate_paths)
                        if not git.commit_paths(owned_paths, commit_subject):
                            write_log(
                                f"iter {iters_run}: HALT — outer Codex commit failed"
                            )
                            output.error(
                                "grind: Codex completed the issue but the outer "
                                "git commit failed",
                                hint="inspect the worktree and commit the completed work manually",
                            )
                            raise typer.Exit(code=1)
                        write_log(f"iter {iters_run}: outer Codex commit completed")
                        transaction_store.clear()
                        active_journal = None
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
                    if resolved_backend == "codex" and active_journal is not None:
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            codex_baseline,
                            phase="orphaned-candidate",
                        )
                        source_candidate = (
                            frozenset(active_journal.candidate_paths)
                            - _TRACKER_EXPORT_PATHS
                        )
                        if source_candidate:
                            write_log(
                                f"iter {iters_run}: preserving owned candidate for "
                                f"{active_journal.issue_id}: {sorted(source_candidate)}"
                            )
                            output.progress(
                                "grind",
                                f"preserved candidate {active_journal.issue_id}; "
                                "re-run grind to resume",
                            )
                            break
                        transaction_store.clear()
                        active_journal = None
                    action = apply_orphan_policy(
                        orphan_policy,
                        delta.orphan_ids,
                        revert_fn=lambda i: bd.update_status(i, "open"),
                        escalate_fn=lambda i: bd.add_label(i, "human"),
                    )
                    for line in action.actions_taken:
                        write_log(f"  orphan-policy: {line}")
                else:
                    if (
                        resolved_backend == "codex"
                        and active_journal is not None
                        and active_journal.issue_id in after.in_progress_ids
                    ):
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            codex_baseline,
                            phase="incomplete-candidate",
                        )
                        write_log(
                            f"iter {iters_run}: preserving incomplete candidate for "
                            f"{active_journal.issue_id}"
                        )
                        output.progress(
                            "grind",
                            f"preserved candidate {active_journal.issue_id}; "
                            "re-run grind to resume",
                        )
                        break
                    write_log(f"iter {iters_run}: WARN no bd-state change (rc={rc})")

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
            if resolved_backend == "codex" and active_journal is None:
                _checkpoint_codex_preflight(
                    git,
                    integration_branch,
                    write_log,
                    allowed_dirty=codex_baseline,
                )
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
