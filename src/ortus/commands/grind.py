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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ortus.core import cache, hooks, output, sandbox
from ortus.core.agent import (
    BackendError,
    compose_worker_prompt,
    make_runner,
    resolve_backend,
)
from ortus.core.bd import BdClient
from ortus.core.claude import (
    REPO_TOOL_STATE,
    ClaudeRunner,
    ReadOnlyExecutionBlocked,
)
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
from ortus.core.transaction import (
    FINALIZATION_STEPS,
    CandidateJournal,
    JournalStore,
)
from ortus.core.transaction import (
    candidate_diff,
    fingerprint_paths,
    issue_packet_hash,
    sha256_bytes,
)
from ortus.core.verdict import (
    Verdict,
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


def _is_tool_state(path: str) -> bool:
    """Whether a dirty path is tool state rather than candidate content.

    The verifier's repo is writable under the inverted posture (ortus-v8fn), so
    the agent CLI's inner sandbox materialises its deny-rule placeholders for
    real — `<repo>/.claude/hooks` and friends. A repo that does not ignore those
    reports them as untracked, which moves the candidate path set and has the
    mutation guard reject an otherwise sound verdict. Observed on two repos.

    Carved out for the same reason `_TRACKER_EXPORT_PATHS` is: written by the
    machinery during a run, never code under test. The set is the one the mount
    leaves writable, imported rather than restated so the two cannot drift.
    """

    return path.split("/", 1)[0] in REPO_TOOL_STATE


def _candidate_paths(dirty: frozenset[str], baseline: frozenset[str]) -> frozenset[str]:
    """The dirty paths a verdict is legitimately about."""

    return frozenset(
        path
        for path in dirty - baseline - _TRACKER_EXPORT_PATHS
        if not _is_tool_state(path)
    )
_EVIDENCE_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)([^\r\n]+)"
)
#: Where a recovery worker declares which inherited changes are not this issue's
#: work. It lives under the already-ignored logs/ tree, so the declaration is
#: never itself a candidate path, and Ortus consumes it into the journal after
#: the worker returns.
_UNRELATED_DECLARATION = Path("logs") / "grind-unrelated-paths.txt"
#: The recovery block shares Claude's 4,000-character /goal budget with the base
#: task and the CodeGraph contract, so it is bounded on both axes.
_HANDOFF_PROMPT_PATHS = 12
_HANDOFF_PROMPT_CHARS = 1_200
#: Journal phases whose candidate was already sealed for review. A path-set
#: difference against one of these is real drift worth reporting to the worker;
#: for an in-flight phase it is just the prior worker's unfinished edits.
_SEALED_PHASES = frozenset(
    {
        "implementation-timeout",
        "verification-timeout",
        "correction-timeout",
        "orphaned-candidate",
        "incomplete-candidate",
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
    paths = _candidate_paths(dirty, baseline)
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


def _candidate_baseline(
    journal: CandidateJournal | None, baseline: frozenset[str]
) -> frozenset[str]:
    """Paths a candidate must never absorb: operator baseline plus disowned work.

    Everything that computes or re-checks candidate ownership — capture,
    finalization, the finalization blocker — must subtract the same set, or a
    path a worker declared unrelated would read as candidate drift.
    """

    if journal is None:
        return baseline
    return baseline | frozenset(journal.unrelated_paths)


def _absorb_unrelated_declaration(
    repo: Path,
    store: JournalStore,
    journal: CandidateJournal,
    write_log: Callable[[str], None],
) -> CandidateJournal:
    """Consume the worker's "this inherited change is not mine" declaration.

    A recovery worker inherits whatever the previous engineer left behind, and
    only the worker can judge which of it belongs to this issue. Declaring a
    path here keeps it in the worktree and out of the candidate, so it is
    neither reset, stashed, deleted, nor committed. The declaration is honored
    for handoff paths only: work this attempt produced cannot be disowned, and
    neither can inherited work the journal attributes to the claimed issue.
    """

    declaration = repo / _UNRELATED_DECLARATION
    try:
        raw = declaration.read_text(encoding="utf-8")
    except OSError:
        return journal
    declared = {line.strip() for line in raw.splitlines()} - {""}
    honored = declared & set(journal.handoff_paths)
    ignored = sorted(declared - honored)
    # The resume exists to carry the prior attempt at *this* issue forward, so
    # its own inherited candidate is not disownable: a worker that declares it
    # would abandon exactly the work it was handed. Refusing is non-fatal — the
    # paths stay in the candidate and the verifier judges the whole of it —
    # because a hard abort turns a recoverable misjudgement into a stopped run.
    own_work = sorted(honored & journal.own_inherited_work())
    honored -= set(own_work)
    try:
        declaration.unlink()
    except OSError:
        pass
    if ignored:
        write_log(
            "handoff: ignoring unrelated declaration outside the inherited work: "
            + ", ".join(ignored[:_HANDOFF_PROMPT_PATHS])
        )
    if own_work:
        write_log(
            "handoff: ignoring unrelated declaration for inherited work belonging "
            f"to {journal.issue_id}; kept in the candidate: "
            + ", ".join(own_work[:_HANDOFF_PROMPT_PATHS])
        )
    if not honored:
        return journal
    updated = journal.with_unrelated(honored)
    store.save(updated)
    write_log(
        "handoff: worker declared unrelated; left untouched and never committed: "
        + ", ".join(sorted(honored))
    )
    output.progress(
        "grind", f"{len(honored)} inherited path(s) declared unrelated; left untouched"
    )
    return updated


@dataclass
class _HandoffState:
    """What this run inherited: the goal, the work on disk, and what moved.

    An unsuccessful run leaves two things behind — the assigned issue and the
    uncommitted worktree — and together they are the handoff artifact. This
    carries both to the iteration that resumes them, plus the drift notes that
    are context for the model rather than startup failures.
    """

    journal: CandidateJournal | None = None
    handoff_paths: frozenset[str] = frozenset()
    disowned: frozenset[str] = frozenset()
    resume_issue_id: str | None = None
    candidate_ready: bool = False
    active: bool = False
    prior_phase: str = ""
    diff_ref: str = ""
    notes: tuple[str, ...] = ()

    def instruction(self) -> str:
        """The bounded recovery contract appended to the worker's phase rules."""

        listed = sorted(self.handoff_paths)[:_HANDOFF_PROMPT_PATHS]
        hidden = len(self.handoff_paths) - len(listed)
        rendered = ", ".join(listed) + (f", +{hidden} more" if hidden > 0 else "")
        text = (
            " RECOVERY HANDOFF: a prior engineer left uncommitted work"
            + (f" and stopped at {self.prior_phase}" if self.prior_phase else "")
            + ". Run `git status` and `git diff HEAD` for the live state"
            + (f"; the captured diff is {self.diff_ref}" if self.diff_ref else "")
            + ". Inherited paths: "
            + (rendered or "none")
            + ". Assess every change against the issue packet: keep and finish what "
            "advances it, correct what is wrong, and leave unrelated work untouched — "
            f"list each unrelated path in {_UNRELATED_DECLARATION.as_posix()}, one per "
            "line, and Ortus will never commit it. Continue from this state instead of "
            "restarting."
        )
        if self.disowned:
            text += (
                " Already declared unrelated by an earlier worker; leave exactly as they "
                "are: " + ", ".join(sorted(self.disowned)[:_HANDOFF_PROMPT_PATHS]) + "."
            )
        if self.notes:
            text += " Prior state: " + "; ".join(self.notes[:3]) + "."
        if len(text) > _HANDOFF_PROMPT_CHARS:
            text = text[:_HANDOFF_PROMPT_CHARS].rstrip() + " [truncated]"
        return text


def _rebuild_journal_from_claim(
    bd: BdClient,
    git: GitClient,
    store: JournalStore,
    *,
    repo: Path,
    write_log: Callable[[str], None],
) -> CandidateJournal | None:
    """Recover the goal from bd when the journal itself is unreadable.

    A single in-progress issue identifies the work well enough to resume it; the
    worktree supplies the rest. Genuine ambiguity about which issue owns the
    handoff is the one case that needs a human, and it is handled by the caller.
    """

    dirty = git.dirty_paths() if git.is_git_repo() else frozenset()
    claimed = bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS)
    if dirty is None or len(claimed) != 1:
        return None
    issue_hint = next(iter(claimed))
    paths = dirty - _TRACKER_EXPORT_PATHS
    try:
        digest, diff_ref = store.save_diff(candidate_diff(repo, paths))
    except RuntimeError as exc:
        write_log(f"transaction handoff: could not diff the inherited work ({exc})")
        return None
    journal = CandidateJournal.start(
        repo=repo,
        issue_id=issue_hint,
        base_head=git.head_oid(),
        baseline_paths=(),
    ).with_candidate(
        paths, phase="handoff", candidate_hash=digest, diff_ref=diff_ref
    )
    store.save(journal)
    write_log(
        "transaction handoff: rebuilt unusable journal from claimed issue "
        f"{issue_hint} and the current worktree"
    )
    return journal


def _prepare_handoff(
    bd: BdClient,
    git: GitClient,
    store: JournalStore,
    *,
    repo: Path,
    backend: str,
    integration_branch: str,
    write_log: Callable[[str], None],
) -> _HandoffState:
    """Resume the prior goal and hand the current worktree to a fresh worker.

    Any unsuccessful run — nonzero exit, kill, verifier failure, malformed
    verdict — can return with its issue still assigned and its edits still
    uncommitted. Startup therefore prefers that issue over selecting new work,
    and treats every historical mismatch (journal schema, prior HEAD, path set,
    candidate hash) as context to report rather than a reason to refuse. Nothing
    here resets, stashes, or discards anything.
    """

    journal, notes = store.load_state()
    for note in notes:
        write_log(f"transaction handoff: {note}")
    rebuilt = False
    if journal is None and store.path.exists():
        journal = _rebuild_journal_from_claim(
            bd, git, store, repo=repo, write_log=write_log
        )
        rebuilt = journal is not None
        if journal is None:
            notes = (
                *notes,
                "the prior journal was unusable and no single claimed issue named the goal",
            )
            write_log(
                "transaction handoff: journal is unusable and no single claimed issue "
                "identifies the work; continuing with the current worktree as context"
            )
        else:
            notes = (*notes, "the prior journal was rebuilt from bd and the worktree")

    if journal is not None:
        # Finalization replay already ran, so a journal still here owes work. If
        # its issue is finished or gone, the goal it names is not resumable:
        # reopening closed work — or looping on an id bd cannot read — is worse
        # than losing the routing hint, so the tree becomes plain context.
        status = bd.status(journal.issue_id)
        unroutable = (
            "is already closed"
            if status == "closed"
            else "cannot be read from bd"
            if not status
            else ""
        )
        if unroutable:
            write_log(
                f"transaction handoff: {journal.issue_id} {unroutable}; not resuming "
                "it. Its uncommitted work is presented as context instead"
            )
            notes = (
                *notes,
                f"{journal.issue_id} {unroutable} while its candidate was still pending",
            )
            store.clear()
            journal = None

    if journal is None:
        # No routable transaction. Uncommitted work is still an engineering
        # handoff rather than a reason to fence the next worker away from
        # potentially useful code, so it is preserved and presented.
        if backend == "codex":
            inherited = _checkpoint_codex_preflight(
                git, integration_branch, write_log, accept_baseline=True
            )
        else:
            dirty = git.dirty_paths() if git.is_git_repo() else frozenset()
            if dirty is None:
                write_log("transaction handoff: git status failed; assuming clean tree")
                dirty = frozenset()
            inherited = dirty - _TRACKER_EXPORT_PATHS
        if not inherited:
            return _HandoffState(notes=notes)
        state = _HandoffState(handoff_paths=inherited, active=True, notes=notes)
        claimed = bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS)
        if len(claimed) == 1:
            # The claim and the uncommitted work together are the handoff, so
            # prefer that issue over selecting anything new.
            state.resume_issue_id = next(iter(claimed))
            write_log(
                "transaction handoff: resuming the single claimed issue "
                f"{state.resume_issue_id}"
            )
        elif len(claimed) > 1:
            # Genuine ambiguity — several claims could own this work and neither
            # bd nor the worktree decides between them. Preserve everything and
            # ask for routing instead of guessing a goal.
            #
            # This halt deliberately precedes the startup orphan sweep: the
            # sweep would revert or escalate both claims, erasing the very
            # evidence the operator needs to decide which one owns the
            # uncommitted paths. `ortus grind --orphan-policy` is still the way
            # to clear multiple stale claims — but only once the work in the
            # tree has an owner, which is what the hint below asks for.
            rendered = ", ".join(sorted(claimed))
            write_log(
                "transaction handoff: HALT — uncommitted work cannot be routed; "
                f"claimed issues: {rendered}; paths: {', '.join(sorted(inherited))}"
            )
            output.error(
                "grind: uncommitted work with no journal and more than one claimed "
                f"issue ({rendered}); nothing was changed",
                hint=(
                    "decide which issue owns these paths, leave only that one "
                    "in_progress, then re-run grind: "
                    + ", ".join(sorted(inherited))
                ),
            )
            raise typer.Exit(code=1)
        if git.is_git_repo():
            try:
                _, state.diff_ref = store.save_diff(candidate_diff(repo, inherited))
            except RuntimeError as exc:
                write_log(
                    f"transaction handoff: could not capture the inherited diff ({exc})"
                )
        write_log(
            "transaction handoff: presenting existing uncommitted changes to the next "
            "worker for relevance assessment: " + ", ".join(sorted(inherited))
        )
        write_log("transaction handoff: git status\n" + (git.status_text() or "(none)"))
        output.progress(
            "grind",
            f"handing {len(inherited)} uncommitted path(s) to the next worker",
        )
        return state

    prior_phase = journal.phase
    dirty = git.dirty_paths() if git.is_git_repo() else frozenset()
    if dirty is None:
        output.error("grind: could not read the worktree for recovery handoff")
        raise typer.Exit(code=1)
    current_head = git.head_oid() if git.is_git_repo() else ""
    moved = list(notes)
    if current_head != journal.base_head:
        moved.append(
            f"HEAD moved from {journal.base_head[:12] or 'nothing'} to "
            f"{current_head[:12] or 'nothing'}"
        )
    if not journal.baseline_is_unchanged(repo):
        moved.append("the recorded operator baseline changed")
    # The recorded baseline and HEAD are audit evidence from here on, not
    # preconditions: the current tree is what the worker will actually see.
    journal = replace(journal, baseline_paths=(), baseline_fingerprints={})
    # A disowned path that changed since it was disowned has been taken back:
    # someone edited it deliberately, so it belongs to the candidate again
    # rather than being excluded from review forever.
    current_fingerprints = fingerprint_paths(repo, journal.unrelated_paths)
    readopted = sorted(
        path
        for path, digest in journal.handoff_fingerprints.items()
        if path in journal.unrelated_paths and current_fingerprints.get(path) != digest
    )
    if readopted:
        journal = replace(
            journal,
            unrelated_paths=tuple(
                path for path in journal.unrelated_paths if path not in readopted
            ),
        )
        moved.append("previously unrelated work was edited: " + ", ".join(readopted))
    # What the prior worker on this issue actually owned, read before the live
    # candidate is recomputed below. Only this recorded set is attributable: the
    # recomputed one sweeps in whatever else went dirty since — a stranded file
    # from another issue among it — and that must stay disownable. A rebuilt
    # journal inferred its candidate from the worktree rather than recording it,
    # so it attributes nothing.
    recorded_candidate = frozenset() if rebuilt else frozenset(journal.candidate_paths)
    baseline = _candidate_baseline(journal, frozenset())
    candidate = _candidate_paths(dirty, baseline)
    if prior_phase in _SEALED_PHASES and candidate != frozenset(journal.candidate_paths):
        moved.append("the candidate path set changed since the prior worker")
    try:
        diff = candidate_diff(repo, candidate) if git.is_git_repo() else b""
    except RuntimeError as exc:
        moved.append(f"the candidate could not be re-diffed ({exc})")
        diff = b""
    if sha256_bytes(diff) != journal.candidate_hash:
        digest, diff_ref = store.save_diff(diff)
        journal = journal.with_candidate(
            candidate, phase=prior_phase, candidate_hash=digest, diff_ref=diff_ref
        )
        moved.append(f"the candidate changed; refreshed to {digest[:12]}")
    else:
        journal = journal.with_candidate(candidate, phase=prior_phase)
    # Fingerprint the disowned paths too, not just the candidate: that record is
    # the only thing that can later tell "still nobody's work" from "a worker
    # picked it back up", so dropping it would make a disown permanent.
    journal = journal.with_handoff(
        repo=repo,
        paths=candidate | frozenset(journal.unrelated_paths),
        notes=moved,
        base_head=current_head,
        owner=journal.issue_id,
        owned=recorded_candidate,
    )
    store.save(journal)
    if backend == "codex":
        # The recorded candidate is the accepted context now, so ownership must
        # not re-litigate it; tracker exports stay uncommitted for finalization.
        _checkpoint_codex_preflight(
            git,
            integration_branch,
            write_log,
            allowed_dirty=dirty,
            checkpoint_tracker=False,
        )
    drift = moved[len(notes) :]
    if drift:
        write_log(
            "transaction handoff: repository state moved since the prior worker; "
            "adopting the current worktree for model review"
        )
    for note in drift:
        write_log(f"transaction handoff: {note}")
    write_log(
        f"transaction resume: issue={journal.issue_id} phase={prior_phase} "
        f"candidate_paths={sorted(candidate)} "
        f"unrelated={sorted(journal.unrelated_paths)}"
    )
    write_log("transaction handoff: git status\n" + (git.status_text() or "(none)"))
    output.progress(
        "grind",
        f"resuming {journal.issue_id} from {prior_phase} with "
        f"{len(candidate)} inherited path(s)",
    )
    return _HandoffState(
        journal=journal,
        handoff_paths=candidate,
        disowned=frozenset(journal.unrelated_paths),
        resume_issue_id=journal.issue_id,
        # Implementation already produced a reviewable candidate for these
        # phases, so the resume goes straight to a fresh verifier.
        candidate_ready=prior_phase
        in {"candidate-captured", "verification", "verification-timeout"},
        active=True,
        prior_phase=prior_phase,
        diff_ref=journal.candidate_diff_ref,
        notes=tuple(moved),
    )


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
        "EMIT THE VERDICT AS SOON AS EVERY CRITERION CHECK HAS RUN. Investigating "
        "further is welcome only while a verdict is already safe to write, and a "
        "wider sweep you chose to start is never a reason to withhold one. A "
        "session that ends without the line throws away everything it learned: "
        "the run is rejected for reporting no verdict, and a candidate whose "
        "criteria all passed is left uncommitted. If a check could not be run, "
        "fail that criterion and say so in its evidence — that is a verdict too. "
        "If new information arrives after you have decided, fold it into the "
        "evidence and emit; do not restart the review.\n\n"
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
        "the supplied SHA-256 exactly.\n\n"
        "Criterion ids must be exactly the AC-N identifiers listed in the issue packet's "
        "acceptance criteria — every one of them, each used exactly once, and no invented "
        "id of your own. If a check could not be run at all, record that in the evidence "
        "of the criterion it blocks and fail that criterion; do not add a criterion to "
        "carry it.\n\n"
        "Run every pytest sweep you select with `-n auto` so it is distributed across "
        "this host's cores, under the CI gate's `--test-timeout=180`. Most of the wall "
        "clock in this suite is subprocess wait, not computation, so a parallel sweep "
        "returns the same answer several times sooner. Neither flag changes which tests "
        "are selected — select the same tests you would have selected without them, and "
        "never narrow a marker expression to get past them. If this host has no "
        "pytest-xdist, drop `-n auto` and run the identical selection serially.\n\n"
        "`--enforce-duration-budget` is the one CI gate flag you deliberately do not "
        "reproduce. The five-second budget is a claim about how fast a test is on a "
        "quiet machine, and contending workers inflate every duration, so enforcing it "
        "alongside `-n auto` reports breaches that are an artifact of the parallelism. "
        "CI runs the gate single-threaded with the budget enforced and stays the "
        "authority on duration, `slow`-marked tests exempt there exactly as before. "
        "Judge whether the candidate is correct; leave how fast a test is to CI.\n"
        + probe_text
    )


@dataclass
class _VerificationResult:
    """What one fresh verifier attempt produced, for the retry controller."""

    journal: CandidateJournal
    summary: Any
    verdict: Verdict | None = None
    failure: str | None = None
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.failure is None
            and self.verdict is not None
            and self.verdict.passed
            and not self.timed_out
        )


def _verify_candidate(
    *,
    runner: ClaudeRunner,
    bd: BdClient,
    git: GitClient,
    store: JournalStore,
    journal: CandidateJournal,
    repo: Path,
    log: Path,
    write_log: Callable[[str], None],
    backend: str,
    issue_id: str,
    packet: dict,
    profile: AgentProfile,
    worker_timeout: int,
    probe: CodeGraphProbe,
    mode: CodeGraphMode,
    configure_codegraph: Callable[[Any], None] | None,
    baseline: frozenset[str],
    freshness: str,
    sync_ms: int,
    iteration: int,
) -> _VerificationResult:
    """Run one fresh read-only verifier over the current candidate.

    Extracted from `grind()` so the initial attempt and every bounded
    correction attempt run the *identical* verification, isolation, and report
    persistence path — a correction that got a weaker verifier would defeat the
    whole transaction.
    """

    # Preflight before anything is journaled or dispatched. A sandbox that
    # cannot execute commands is not something a correction worker or a
    # planning pass can fix, and both were observed spending attempts on
    # exactly that, so this aborts the run instead of producing a report that
    # reads like a judgement of the code (ortus-dyio).
    preflight = getattr(runner, "preflight_readonly", None)
    if callable(preflight):
        try:
            preflight(repo)
        except ReadOnlyExecutionBlocked as exc:
            bd.update_status(issue_id, "open")
            lines = str(exc).splitlines()
            write_log(f"iter {iteration}: HALT — {lines[0]}")
            output.error(
                f"grind: {lines[0]}",
                hint="\n".join(lines[1:]) or None,
            )
            raise typer.Exit(code=1)

    expected_criteria = dict.fromkeys(
        re.findall(r"\bAC-\d+\b", str(packet.get("acceptance_criteria", "")))
    )
    journal = journal.begin_verification()
    store.save(journal)
    if configure_codegraph is not None:
        configure_codegraph(probe.capability)
    try:
        verification_probe = (
            _codex_codegraph_handshake(
                runner,
                repo=repo,
                log_path=log,
                phase=CodeGraphPhase.VERIFICATION,
                probe=probe,
                profile=profile,
                timeout=(worker_timeout if worker_timeout > 0 else None),
            )
            if backend == "codex"
            else probe
        )
    except CodeGraphUnavailable as exc:
        bd.update_status(issue_id, "open")
        output.error(str(exc))
        raise typer.Exit(code=1)
    if configure_codegraph is not None:
        configure_codegraph(verification_probe.capability)
    verifier_prompt = _verifier_prompt(
        journal, phase_contract(CodeGraphPhase.VERIFICATION, verification_probe)
    )
    verify_offset = log.stat().st_size if log.exists() else 0
    output.progress(
        "grind",
        "verification CodeGraph handshake "
        + ("requested" if probe.available else "fallback active"),
    )
    timed_out = False
    try:
        rc = runner.run(
            verifier_prompt,
            repo=repo,
            log_path=log,
            fast=False,
            profile=profile,
            timeout=(worker_timeout if worker_timeout > 0 else None),
            readonly=True,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        rc = 143
        write_log(f"iter {iteration}: verifier TIMEOUT after {worker_timeout}s")

    def _summarize() -> Any:
        summary = parse_transcript(
            log,
            phase=CodeGraphPhase.VERIFICATION,
            probe=verification_probe,
            start_offset=verify_offset,
        )
        summary.freshness = freshness
        summary.sync_duration_ms = sync_ms
        append_normalized(log, summary)
        return summary

    def _reject(reason: str, *, phase: str, summary: Any) -> _VerificationResult:
        report = render_rejection_report(
            issue_id=issue_id,
            candidate_hash=journal.candidate_hash,
            failure=reason,
            expected_criteria=expected_criteria,
            base_head=journal.base_head,
            issue_packet_hash=journal.issue_packet_hash,
            attempt=journal.attempt,
            profiles=journal.profiles,
        )
        report = bound_report(report + "\n" + summary.report())
        report_ref = store.save_report(
            journal.candidate_hash, report, attempt=journal.attempt
        )
        bd.add_comment(issue_id, report)
        updated = journal.finish_verification(report_ref, phase=phase)
        store.save(updated)
        _append_verdict_event(
            log,
            decision="rejected",
            candidate_hash=updated.candidate_hash,
            reason=reason,
        )
        return _VerificationResult(
            journal=updated,
            summary=summary,
            failure=reason,
            timed_out=timed_out,
        )

    if timed_out:
        timeout_failure = f"verifier timed out after {worker_timeout}s without a verdict"
        timeout_phase = "verification-timeout"
        if git.is_git_repo():
            post_dirty = git.dirty_paths()
            if post_dirty is None:
                timeout_failure += "; could not inspect the candidate after timeout"
                timeout_phase = "verification-rejected"
            else:
                post_paths = _candidate_paths(post_dirty, baseline)
                if post_paths != frozenset(journal.candidate_paths):
                    timeout_failure += "; verifier mutated the candidate path set"
                    timeout_phase = "verification-rejected"
                else:
                    try:
                        post_diff = candidate_diff(repo, post_paths)
                    except RuntimeError as exc:
                        timeout_failure += f"; could not hash the candidate: {exc}"
                        timeout_phase = "verification-rejected"
                    else:
                        if sha256_bytes(post_diff) != journal.candidate_hash:
                            timeout_failure += "; verifier mutated the candidate"
                            timeout_phase = "verification-rejected"
        current_packet = bd.show(issue_id)
        if issue_packet_hash(current_packet) != journal.issue_packet_hash:
            timeout_failure += (
                "; authoritative issue packet changed during verification"
            )
            timeout_phase = "verification-rejected"
            if current_packet.get("status") != "in_progress":
                bd.update_status(issue_id, "in_progress")
        result = _reject(timeout_failure, phase=timeout_phase, summary=_summarize())
        write_log(
            f"iter {iteration}: preserved verifier-timeout candidate for "
            f"{result.journal.issue_id}: {list(result.journal.candidate_paths)}"
        )
        output.progress(
            "grind",
            f"preserved timed-out candidate {result.journal.issue_id}; "
            "re-run grind to resume",
        )
        return result

    if backend == "claude":
        rejection = _claude_goal_rejection(log, start_offset=verify_offset)
        if rejection is not None:
            bd.update_status(issue_id, "open")
            write_log(
                f"iter {iteration}: HALT — Claude rejected verifier /goal before "
                f"running a worker turn: {rejection}"
            )
            output.error(
                "grind: Claude rejected the verifier /goal condition before worker work",
                hint=rejection,
            )
            raise typer.Exit(code=1)

    summary = _summarize()
    if summary.capability_observed:
        output.progress("grind", "verification CodeGraph handshake succeeded")
    elif mode is not CodeGraphMode.OFF:
        output.progress(
            "grind",
            "verification CodeGraph fallback: " + "; ".join(summary.fallbacks[:3]),
        )
    try:
        require_handshake(summary)
    except CodeGraphUnavailable as exc:
        bd.update_status(issue_id, "open")
        output.error(str(exc))
        raise typer.Exit(code=1)

    failure: str | None = None
    verdict: Verdict | None = None
    try:
        verdict = parse_verdict(
            log,
            start_offset=verify_offset,
            expected_hash=journal.candidate_hash,
            expected_criteria=expected_criteria,
        )
        if git.is_git_repo():
            post_dirty = git.dirty_paths()
            if post_dirty is None:
                raise VerdictError(
                    "could not inspect the candidate after verification"
                )
            post_paths = _candidate_paths(post_dirty, baseline)
            if post_paths != frozenset(journal.candidate_paths):
                raise VerdictError(
                    "verifier mutated the candidate path set during read-only review"
                )
            post_diff = candidate_diff(repo, post_paths)
            if sha256_bytes(post_diff) != journal.candidate_hash:
                raise VerdictError(
                    "verifier mutated the candidate during read-only review"
                )
        current_packet = bd.show(issue_id)
        if issue_packet_hash(current_packet) != journal.issue_packet_hash:
            raise VerdictError(
                "authoritative issue packet changed during verification"
            )
    except (VerdictError, RuntimeError) as exc:
        failure = str(exc)
    if rc != 0 and failure is None:
        failure = f"verifier exited with status {rc}"

    if verdict is None or failure is not None:
        assert failure is not None
        # A verifier is observational only. A fake/misconfigured runner that
        # changed lifecycle state cannot make its own output authoritative;
        # restore the claim before persisting the rejection.
        if bd.show(issue_id).get("status") != "in_progress":
            bd.update_status(issue_id, "in_progress")
        result = _reject(failure, phase="verification-rejected", summary=summary)
        write_log(f"iter {iteration}: verifier rejected: {failure}")
        output.error(f"grind: verifier rejected candidate: {failure}")
        return result

    report = render_report(
        verdict,
        issue_id=issue_id,
        base_head=journal.base_head,
        issue_packet_hash=journal.issue_packet_hash,
        attempt=journal.attempt,
        profiles=journal.profiles,
    )
    report = bound_report(report + "\n" + summary.report())
    report_ref = store.save_report(
        journal.candidate_hash, report, attempt=journal.attempt
    )
    # AC-1: the complete failed report is durable BEFORE any correction runs.
    bd.add_comment(issue_id, report)
    journal = journal.finish_verification(
        report_ref, phase=("verified-pass" if verdict.passed else "verified-fail")
    )
    store.save(journal)
    _append_verdict_event(
        log, decision=verdict.decision, candidate_hash=journal.candidate_hash
    )
    write_log(
        f"iter {iteration}: verifier verdict={verdict.decision} "
        f"candidate={journal.candidate_hash}"
    )
    return _VerificationResult(journal=journal, summary=summary, verdict=verdict)


_PLAN_GAP_MARKER = re.compile(r"(?i)plan[\s_-]?gap")
_CORRECTION_ENTRY_CHARS = 400
_CORRECTION_MAX_FINDINGS = 6


def _plan_gap_findings(verdict: Verdict) -> tuple[str, ...]:
    """Findings the verifier attributed to an unresolved planning decision.

    A fast correction worker may not invent product or architecture answers,
    so these route once to the planning profile (or a human) instead of
    spending a correction attempt on improvisation.
    """

    return tuple(
        finding for finding in verdict.findings if _PLAN_GAP_MARKER.search(finding)
    )


def _failed_criteria(verdict: Verdict) -> tuple[dict[str, str], ...]:
    return tuple(item for item in verdict.criteria if item["status"] == "fail")


def _correction_task(issue_id: str, journal: CandidateJournal, verdict: Verdict) -> str:
    """The minimal correction packet: issue, hash, failed criteria, findings.

    Deliberately excludes the verifier transcript and the full report. The
    worker re-reads the authoritative packet from bd; everything else here is
    the precise delta it has to close.
    """

    criteria = "\n".join(
        f"- {item['id']}: {item['evidence'][:_CORRECTION_ENTRY_CHARS]}"
        for item in _failed_criteria(verdict)
    ) or "- (the verifier recorded no criterion-level failure)"
    findings = [
        f"- {finding[:_CORRECTION_ENTRY_CHARS]}"
        for finding in verdict.findings[:_CORRECTION_MAX_FINDINGS]
    ] or ["- (no findings recorded)"]
    header = (
        f"CORRECTION ATTEMPT {journal.corrections} for bd issue {issue_id}. A fresh "
        "read-only verifier rejected the current candidate. Ortus already claimed this "
        "issue; do not run bd ready, do not select other work, and use only the id "
        f"{issue_id}. Read `bd show {issue_id} --json` for the authoritative packet, "
        "then correct ONLY the failures below.\n\n"
        f"Current candidate SHA-256: {journal.candidate_hash}\n\n"
        f"Failed acceptance criteria:\n{criteria}\n\n"
    )
    footer = (
        "\n\nKeep the existing candidate edits and correct them in place. Do not close "
        "the issue, do not run git commit, git push, git stash, or git reset, do not "
        "switch branches, and do not add a verification comment — a fresh verifier "
        "reviews your corrected candidate and Ortus alone finalizes it. If a finding "
        "needs a product or architecture decision the packet does not resolve, do not "
        "improvise: report it as a PLAN-GAP and stop."
    )
    # Claude's /goal condition is hard-capped, so drop the least-severe findings
    # (verifiers order them most-severe-first) rather than truncating mid-word
    # into an unreadable instruction.
    while findings:
        task = header + "Verifier findings:\n" + "\n".join(findings) + footer
        if len(task) <= _CLAUDE_GOAL_CONDITION_LIMIT - 200 or len(findings) == 1:
            return task
        findings.pop()
    return header + footer


def _compose_correction_prompt(
    issue_id: str, journal: CandidateJournal, verdict: Verdict, backend: str
) -> str:
    return compose_worker_prompt(  # type: ignore[arg-type]
        backend, _correction_task(issue_id, journal, verdict)
    )


def _plan_gap_task(issue_id: str, findings: tuple[str, ...]) -> str:
    rendered = "\n".join(f"- {finding[:_CORRECTION_ENTRY_CHARS]}" for finding in findings)
    return (
        "PLAN-GAP ROUTING PASS (one pass only).\n\n"
        f"Verification of bd issue {issue_id} surfaced findings that need a product or "
        "architecture decision the issue packet never resolved. An implementation "
        "worker is not allowed to improvise them.\n\n"
        f"Unresolved findings:\n{rendered}\n\n"
        f"Read `bd show {issue_id} --json`, resolve each gap against repository "
        "reality, and update ONLY that issue in place with `bd update` so its "
        "description, design, and acceptance criteria state the resolved decision. "
        "Do not run `bd create`, do not close, replace, or rename any issue, do not "
        "change dependencies, do not edit source files, and do not commit or push. If "
        "the decision genuinely requires a human, leave the packet unchanged and say "
        "so plainly."
    )


def _run_plan_gap_pass(
    bd: BdClient,
    *,
    repo: Path,
    log: Path,
    write_log: Callable[[str], None],
    backend: str,
    profile: AgentProfile,
    probe: CodeGraphProbe,
    timeout: int | None,
    issue_id: str,
    findings: tuple[str, ...],
) -> int:
    """Route one plan gap through the planning profile; return its exit code.

    Mirrors the readiness-repair guard: a pass that grows the queue instead of
    updating the named issue is a hard error, not a silent skip.
    """

    gap_log = log.with_name(f"{log.stem}-plan-gap{log.suffix}")
    # Mirror the factory call shape the readiness-repair pass uses so the
    # zero-argument Claude seam existing test overrides rely on keeps working.
    runner = _make_runner() if backend == "claude" else _make_runner("codex")
    configure = getattr(runner, "configure_codegraph", None)
    if callable(configure):
        configure(probe.capability)
    prompt = compose_worker_prompt(  # type: ignore[arg-type]
        backend,
        _plan_gap_task(issue_id, findings)
        + phase_contract(CodeGraphPhase.PLANNING, probe),
    )
    ids_before = {issue["id"] for issue in bd.list_all()}
    try:
        if timeout is None:
            rc = runner.run(prompt, repo=repo, log_path=gap_log, profile=profile)
        else:
            rc = runner.run(
                prompt, repo=repo, log_path=gap_log, profile=profile, timeout=timeout
            )
    except subprocess.TimeoutExpired:
        write_log(f"plan-gap routing: TIMEOUT after {timeout}s; see {gap_log}")
        return 143
    guard_no_replacements(ids_before, {issue["id"] for issue in bd.list_all()})
    if rc != 0:
        write_log(f"plan-gap routing: failed ({backend} exit {rc}); see {gap_log}")
    return rc


_FINALIZATION_MARKER = "## Ortus finalization record"
_FINALIZATION_BLOCKED_PHASE = "finalization-blocked"
#: Journal phases whose transaction still owes finalization work. A restart at
#: any of these resumes the remaining steps instead of selecting new work.
#:
#: ``finalization-blocked`` belongs here too. A blocker is usually transient —
#: an operator edit outside the transaction, a tree left on the wrong branch —
#: and the outstanding steps are tracked by `journal.finalized(step)`, not by
#: this label, so replaying is safe and resumes exactly where it stopped. Left
#: out, a transaction blocked after its close would never commit: the operator
#: would clear the blocker, re-run, and grind would silently skip the pending
#: work and pick up another issue, stranding verified work behind a closed
#: issue (AC-5 requires the opposite — hold the queue until it finishes).
_FINALIZABLE_PHASES = frozenset(
    {
        "verified-pass",
        _FINALIZATION_BLOCKED_PHASE,
        *(f"finalized-{step}" for step in FINALIZATION_STEPS[:-1]),
    }
)


def _finalization_report(issue_id: str, journal: CandidateJournal) -> str:
    return (
        f"{_FINALIZATION_MARKER}\n\n"
        f"Issue: {issue_id}\n"
        f"Candidate: `{journal.candidate_hash}`\n"
        f"Base commit: `{journal.base_head}`\n"
        f"Issue packet: `{journal.issue_packet_hash}`\n"
        f"Verifier attempts: {len(journal.verifier_refs)}\n"
        f"Correction attempts: {journal.corrections}\n"
        f"Plan-gap routed: {'yes' if journal.plan_gap_routed else 'no'}\n"
        f"Verifier reports: {', '.join(journal.verifier_refs) or 'none'}\n"
        f"Owned paths: {', '.join(journal.candidate_paths) or 'none'}\n\n"
        "A fresh read-only verifier passed this exact candidate. Ortus — not the "
        "agent — wrote this record, closed the issue, committed the owned paths, and "
        "synchronized the integration branch.\n"
    )


def _finalization_blocker(
    bd: BdClient,
    git: GitClient,
    journal: CandidateJournal,
    *,
    repo: Path,
    issue_id: str,
    integration_branch: str,
    baseline: frozenset[str],
) -> str | None:
    """Re-validate every precondition a passing verdict is allowed to assume.

    Runs before the first *un-journaled* step, and re-checks only what a
    partially-finalized transaction can still assert: once the commit landed,
    HEAD legitimately differs from the recorded base and the candidate is no
    longer in the worktree.
    """

    if journal.issue_id != issue_id:
        return (
            f"journal owns {journal.issue_id} but the iteration claimed {issue_id}"
        )
    if not journal.verifier_refs:
        return "no verifier report was persisted for this candidate"
    if not journal.candidate_hash:
        return "candidate transaction has no recorded hash"
    # Identity and status only: the verifier already bound its verdict to the
    # authoritative packet hash, and grind's own report comment legitimately
    # moves the issue's `bd show` bytes between that check and this one.
    status = bd.status(issue_id)
    if not journal.finalized("close") and status != "in_progress":
        return f"issue is {status or 'unreadable'}, not the claimed in_progress state"
    if journal.finalized("close") and status != "closed":
        return f"issue was closed by this transaction but now reads {status or 'unreadable'}"
    if not git.is_git_repo():
        return None
    branch = git.current_branch()
    # An unborn branch (bd-only fixture, freshly `ortus init`'d repo) has no
    # resolvable name and nothing stranded; branch discipline skips it for the
    # same reason, so finalization must not read it as a detached HEAD.
    if git.has_commits() and branch != integration_branch:
        return (
            f"working tree is on {branch or 'a detached HEAD'}, "
            f"not {integration_branch}"
        )
    if journal.finalized("commit"):
        return None
    if git.head_oid() != journal.base_head:
        return (
            f"base commit {journal.base_head} is no longer HEAD; the candidate was "
            "verified against a different tree"
        )
    dirty = git.dirty_paths()
    if dirty is None:
        return "could not read the worktree before finalization"
    owned = _candidate_paths(dirty, baseline)
    if owned != frozenset(journal.candidate_paths):
        drifted = sorted(owned.symmetric_difference(journal.candidate_paths))
        return (
            "candidate path set changed after the passing verdict: "
            + ", ".join(drifted)
        )
    try:
        if sha256_bytes(candidate_diff(repo, owned)) != journal.candidate_hash:
            return "candidate changed after the passing verdict"
    except RuntimeError as exc:
        return f"could not re-hash the candidate ({exc})"
    return None


def _finalize_candidate(
    bd: BdClient,
    git: GitClient,
    store: JournalStore,
    journal: CandidateJournal,
    *,
    repo: Path,
    issue_id: str,
    integration_branch: str,
    baseline: frozenset[str],
    write_log: Callable[[str], None],
) -> tuple[CandidateJournal, str | None]:
    """Ortus-owned report → close → commit → sync, journaled step by step.

    Returns the updated journal and a blocker string when finalization could
    not complete. Every step is skipped when its boundary is already journaled
    OR when observable state already shows it landed, so a restart at any
    boundary produces no duplicate comment, close, commit, or push.
    """

    blocker = _finalization_blocker(
        bd,
        git,
        journal,
        repo=repo,
        issue_id=issue_id,
        integration_branch=integration_branch,
        baseline=baseline,
    )
    if blocker is not None:
        journal = replace(journal, phase=_FINALIZATION_BLOCKED_PHASE)
        store.save(journal)
        return journal, blocker

    owned_paths = frozenset(journal.candidate_paths)

    if not journal.finalized("report"):
        try:
            if not bd.has_comment(issue_id, _FINALIZATION_MARKER):
                bd.add_comment(issue_id, _finalization_report(issue_id, journal))
        except Exception as exc:
            return journal, f"final report could not be persisted ({exc})"
        journal = journal.with_finalization("report")
        store.save(journal)
        write_log(f"finalization: report persisted for {issue_id}")

    if not journal.finalized("close"):
        try:
            closed = bd.close_once(
                issue_id,
                reason=(
                    "verified by a fresh read-only Ortus verifier "
                    f"(candidate {journal.candidate_hash[:12]}, "
                    f"{journal.corrections} correction attempt(s))"
                ),
            )
        except Exception as exc:
            return journal, f"bd close failed ({exc})"
        journal = journal.with_finalization("close")
        store.save(journal)
        write_log(
            f"finalization: {issue_id} "
            + ("closed by Ortus" if closed else "was already closed; close skipped")
        )

    if not journal.finalized("commit"):
        if not git.is_git_repo():
            journal = journal.with_finalization("commit", "not-a-git-repo")
            store.save(journal)
        else:
            dirty = git.dirty_paths()
            if dirty is None:
                return journal, "could not read the worktree before committing"
            # Closing the issue rewrites the generated tracker exports; they are
            # Ortus-owned output of this transaction, so they ride along. Nothing
            # else does — never `git add -A` over an operator's unrelated work.
            stage = (owned_paths | (dirty & _TRACKER_EXPORT_PATHS)) & dirty
            unrelated = dirty - stage - _TRACKER_EXPORT_PATHS
            if unrelated:
                write_log(
                    "finalization: leaving unrelated worktree paths untouched: "
                    + ", ".join(sorted(unrelated))
                )
            subject = f"{issue_id}: verified candidate"
            if not git.commit_paths(stage, subject):
                return journal, "path-scoped commit of the owned candidate failed"
            journal = journal.with_finalization("commit", git.head_oid())
            store.save(journal)
            write_log(
                f"finalization: committed owned paths for {issue_id}: "
                + (", ".join(sorted(stage)) or "none")
            )

    if not journal.finalized("sync"):
        if not git.is_git_repo() or not git.has_remote():
            journal = journal.with_finalization("sync", "no-remote")
            store.save(journal)
            write_log("finalization: no remote configured; nothing to push")
        else:
            pushed = git.push(integration_branch)
            if not pushed:
                write_log(
                    "finalization: push rejected; pulling --rebase and retrying once"
                )
                if git.pull_rebase(integration_branch):
                    pushed = git.push(integration_branch)
            if not pushed:
                return journal, (
                    f"push of {integration_branch} to origin failed; the close and "
                    "commit are recorded locally but are NOT on origin"
                )
            if git.local_ahead_of_remote(integration_branch):
                return journal, (
                    f"{integration_branch} is still ahead of origin after a "
                    "successful push"
                )
            journal = journal.with_finalization("sync", "pushed")
            store.save(journal)
            write_log(f"finalization: {integration_branch} synchronized with origin")

    store.clear()
    return journal, None


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
        help=(
            "Legacy: custom per-iteration /goal condition whose worker also "
            "selects its own issue (replaces the harness-claimed work-issue.txt "
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
        # stopped. A worker now runs the packet's targeted suite during
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
            "claimed candidate for restart; Claude runs bd-state/orphan-policy "
            "recovery. 0 disables the watchdog (workers may then hang indefinitely)."
        ),
    ),
    max_corrections: int = typer.Option(
        2,
        "--max-corrections",
        help=(
            "Bounded fresh correction attempts after a failed verification "
            "(0 disables retries). Each attempt spawns one fresh implementation "
            "worker with only the failed criteria and findings, then one fresh "
            "verifier. Exhaustion flags the issue for a human and never commits."
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
        output.info(
            "corrections:    "
            + (
                f"up to {max_corrections} fresh attempt(s), each re-verified"
                if max_corrections > 0
                else "off (a failed verdict escalates immediately)"
            )
        )
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
            # AC-6: a run killed between any two finalization boundaries left a
            # journal that still owes work. Replay it BEFORE selecting anything,
            # and never select another issue while one is outstanding. Each step
            # re-checks observable bd/git state, so a replay of a boundary that
            # actually landed is a no-op rather than a duplicate.
            pending_finalization = transaction_store.load()
            if (
                pending_finalization is not None
                and pending_finalization.phase in _FINALIZABLE_PHASES
            ):
                write_log(
                    "finalization resume: journal for "
                    f"{pending_finalization.issue_id} stopped at "
                    f"{pending_finalization.phase}; replaying remaining boundaries"
                )
                output.progress(
                    "grind",
                    f"resuming finalization of {pending_finalization.issue_id}",
                )
                pending_finalization, blocker = _finalize_candidate(
                    bd,
                    git,
                    transaction_store,
                    pending_finalization,
                    repo=target,
                    issue_id=pending_finalization.issue_id,
                    integration_branch=integration_branch,
                    # The operator baseline this transaction started from, not
                    # an empty set: grind supports resuming into a dirty tree,
                    # so pre-existing unrelated edits must stay excluded from
                    # the owned-path comparison or every such resume would
                    # block as a phantom "candidate path set changed".
                    baseline=_candidate_baseline(
                        pending_finalization,
                        frozenset(pending_finalization.baseline_paths),
                    ),
                    write_log=write_log,
                )
                if blocker is not None:
                    write_log(f"finalization resume: HALT — {blocker}")
                    output.error(
                        f"grind: could not finish the pending finalization — {blocker}",
                        hint=(
                            "the transaction journal under logs/ retains the "
                            "recoverable state; resolve the blocker and re-run grind"
                        ),
                    )
                    raise typer.Exit(code=1)
                write_log(
                    "finalization resume: completed for "
                    f"{pending_finalization.issue_id}"
                )
            # Any unsuccessful exit after a claim leaves the assigned issue and
            # its uncommitted edits behind. Resume that pair — for either
            # backend — before considering new work (AC-1, AC-2).
            handoff = _prepare_handoff(
                bd,
                git,
                transaction_store,
                repo=target,
                backend=resolved_backend,
                integration_branch=integration_branch,
                write_log=write_log,
            )
            active_journal: CandidateJournal | None = handoff.journal
            codex_baseline = frozenset[str]()
            startup_handoff_paths = handoff.handoff_paths
            resume_issue_id = handoff.resume_issue_id
            resume_candidate_ready = handoff.candidate_ready
            recovery_handoff = handoff.active
            # Run-scoped, because a disowned path outlives the transaction that
            # disowned it: it is still in the tree, and the next issue's
            # candidate must not absorb and commit it either.
            disowned_paths = frozenset(
                handoff.journal.unrelated_paths if handoff.journal else ()
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
            #
            # A journal-backed transaction is the one exemption: Ortus owns
            # that claim, knows exactly which candidate it covers, and is about
            # to resume it, so --orphan-policy is not what governs its bd
            # lifecycle. A bare claim with no journal is precisely the
            # cross-restart orphan the policy exists for, and it stays under
            # the policy even when the handoff resumes its goal — the routing
            # hint was captured above before any sweep runs, so `revert` costs
            # nothing (the loop re-claims the same issue) and the operator
            # keeps warn|revert|escalate on the state grind cannot explain.
            orphan_ids = initial_snapshot.in_progress_ids - (
                {handoff.journal.issue_id} if handoff.journal is not None else set()
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
                # True once Ortus itself completed report/close/commit/sync for
                # this iteration, so the legacy worker-owned commit path stays
                # out of the way.
                finalized = False
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
                    allowed = codex_baseline | startup_handoff_paths | disowned_paths
                    if active_journal is not None:
                        allowed |= frozenset(active_journal.candidate_paths)
                        # Disowned work is deliberately outside the candidate; it
                        # is still expected to sit in the tree untouched.
                        allowed |= frozenset(active_journal.unrelated_paths)
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
                    phase_profiles = {
                        "implementation": implement_profile.display_name,
                        "verification": verify_profile.display_name,
                    }
                    packet_digest, packet_ref = transaction_store.save_packet(
                        issue_id, target_issue
                    )
                    # A resumed transaction keeps its journal: it carries the
                    # inherited work, the disowned paths, and the prior evidence
                    # this iteration is continuing from. Anything else — a first
                    # claim, or a journal that owns a different issue — starts
                    # fresh.
                    if active_journal is None or active_journal.issue_id != issue_id:
                        if active_journal is not None:
                            write_log(
                                "transaction handoff: journal owned "
                                f"{active_journal.issue_id} but this iteration claimed "
                                f"{issue_id}; starting a new transaction"
                            )
                        active_journal = CandidateJournal.start(
                            repo=target,
                            issue_id=issue_id,
                            base_head=git.head_oid(),
                            baseline_paths=codex_baseline,
                            packet_hash=packet_digest,
                            packet_ref=packet_ref,
                            profiles=phase_profiles,
                        )
                        if recovery_handoff and startup_handoff_paths:
                            # Inherited work with no routable journal: record what
                            # the worker is being shown so it can disown any of it.
                            active_journal = active_journal.with_handoff(
                                repo=target,
                                paths=startup_handoff_paths | disowned_paths,
                                notes=handoff.notes,
                            )
                        if disowned_paths:
                            active_journal = active_journal.with_unrelated(
                                disowned_paths
                            )
                        transaction_store.save(active_journal)
                    else:
                        if not active_journal.issue_packet_hash:
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
                        output.error("grind: could not record candidate ownership")
                        raise typer.Exit(code=1)
                    active_journal = active_journal.with_candidate(
                        dirty_after_claim
                        - _candidate_baseline(active_journal, codex_baseline)
                        - _TRACKER_EXPORT_PATHS,
                        phase=active_journal.phase
                        if resume_candidate_ready
                        else "implementation",
                    )
                    transaction_store.save(active_journal)
                    resume_issue_id = None
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
                        implementation_instruction += handoff.instruction()
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

                # A recovery worker may have judged some inherited work unrelated
                # to this issue. Honor that before any ownership snapshot, so a
                # declared path never enters the candidate and can never be
                # committed (AC-5).
                if active_journal is not None and not resume_candidate_ready:
                    active_journal = _absorb_unrelated_declaration(
                        target, transaction_store, active_journal, write_log
                    )
                    disowned_paths |= frozenset(active_journal.unrelated_paths)

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
                            _candidate_baseline(active_journal, codex_baseline),
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
                        _candidate_baseline(active_journal, codex_baseline),
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
                            _candidate_baseline(active_journal, codex_baseline),
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
                    verify_kwargs = dict(
                        runner=runner,
                        bd=bd,
                        git=git,
                        store=transaction_store,
                        repo=target,
                        log=log,
                        write_log=write_log,
                        backend=resolved_backend,
                        issue_id=issue_id,
                        packet=target_issue,
                        profile=verify_profile,
                        worker_timeout=worker_timeout,
                        probe=codegraph_probe,
                        mode=codegraph_mode,
                        configure_codegraph=(
                            configure_codegraph
                            if callable(configure_codegraph)
                            else None
                        ),
                        baseline=_candidate_baseline(active_journal, codex_baseline),
                        iteration=iters_run,
                    )
                    verification = _verify_candidate(
                        journal=active_journal,
                        freshness=freshness,
                        sync_ms=sync_ms,
                        **verify_kwargs,
                    )
                    active_journal = verification.journal
                    verification_summary = verification.summary

                    # --- bounded correction retries (AC-1..AC-3) --------------
                    # Every failed verdict is already persisted as a bd comment
                    # by _verify_candidate before this loop can spend an
                    # attempt, so a correction worker always follows a durable
                    # report and never a transcript-only finding.
                    halt: str | None = None
                    halt_phase: str | None = None
                    escalate = False
                    while halt is None:
                        if verification.timed_out or verification.failure is not None:
                            halt = verification.failure or "verifier produced no verdict"
                            # A rejection on the FIRST attempt is ordinary
                            # transaction failure: the candidate keeps its claim
                            # and the next grind resumes it. A rejection after a
                            # correction was already spent means the retry budget
                            # is being burned on something a fresh worker cannot
                            # fix, so flag it rather than leaving a silently
                            # stalled in_progress claim behind.
                            if active_journal.corrections > 0:
                                halt_phase = "correction-rejected"
                                escalate = True
                            break
                        verdict = verification.verdict
                        assert verdict is not None
                        if verdict.passed:
                            break
                        gaps = _plan_gap_findings(verdict)
                        if gaps and active_journal.plan_gap_routed:
                            halt = "plan gap persists after one planning-profile pass"
                            halt_phase = "plan-gap-escalated"
                            escalate = True
                            break
                        if gaps:
                            # AC-3: a material planning gap is never handed to a
                            # fast implementer to improvise; it gets exactly one
                            # planning route, then a human.
                            write_log(
                                f"iter {iters_run}: plan gap in verifier findings; "
                                "routing once through the planning profile"
                            )
                            output.progress(
                                "grind",
                                f"routing plan gap for {issue_id} to the planning "
                                "profile (one pass)",
                            )
                            active_journal = active_journal.route_plan_gap()
                            transaction_store.save(active_journal)
                            try:
                                gap_rc = _run_plan_gap_pass(
                                    bd,
                                    repo=target,
                                    log=log,
                                    write_log=write_log,
                                    backend=resolved_backend,
                                    profile=plan_profile,
                                    probe=codegraph_probe,
                                    timeout=(
                                        worker_timeout if worker_timeout > 0 else None
                                    ),
                                    issue_id=issue_id,
                                    findings=gaps,
                                )
                            except RepairCreatedReplacements as exc:
                                write_log(f"plan-gap routing: HALT — {exc}")
                                output.error(str(exc))
                                raise typer.Exit(code=1)
                            halt = (
                                "routed one plan gap to the planning profile"
                                if gap_rc == 0
                                else f"plan-gap routing pass failed (exit {gap_rc})"
                            )
                            halt_phase = "plan-gap-routed"
                            escalate = True
                            break
                        if active_journal.corrections >= max_corrections:
                            halt = (
                                "bounded correction attempts exhausted "
                                f"({active_journal.corrections}/{max_corrections})"
                            )
                            halt_phase = "corrections-exhausted"
                            escalate = True
                            break

                        active_journal = active_journal.begin_correction(
                            findings=verdict.findings[:_CORRECTION_MAX_FINDINGS]
                        )
                        transaction_store.save(active_journal)
                        write_log(
                            f"iter {iters_run}: correction attempt "
                            f"{active_journal.corrections}/{max_corrections} for "
                            f"{issue_id} (candidate {active_journal.candidate_hash})"
                        )
                        output.progress(
                            "grind",
                            f"correction attempt {active_journal.corrections}/"
                            f"{max_corrections} for {issue_id}",
                        )
                        correction_offset = log.stat().st_size if log.exists() else 0
                        correction_timed_out = False
                        try:
                            rc = runner.run(
                                _compose_correction_prompt(
                                    issue_id,
                                    active_journal,
                                    verdict,
                                    resolved_backend,
                                ),
                                repo=target,
                                log_path=log,
                                fast=fast,
                                profile=implement_profile,
                                timeout=(
                                    worker_timeout if worker_timeout > 0 else None
                                ),
                            )
                        except subprocess.TimeoutExpired:
                            correction_timed_out = True
                            rc = 143
                            write_log(
                                f"iter {iters_run}: correction worker TIMEOUT after "
                                f"{worker_timeout}s"
                            )
                        # A correction worker inherits the same tree, so it can
                        # disown inherited work too. Honor that here, before the
                        # capture below snapshots ownership, or the declaration
                        # would sit unread until some later resume — and the
                        # path it disowned would be committed in the meantime.
                        active_journal = _absorb_unrelated_declaration(
                            target, transaction_store, active_journal, write_log
                        )
                        disowned_paths |= frozenset(active_journal.unrelated_paths)
                        # The re-verify below subtracts this baseline to decide
                        # whether the read-only verifier moved anything. Leaving
                        # it at the pre-disown value would read the newly
                        # excluded path as verifier tampering and reject a
                        # candidate the correction worker just cleaned up.
                        verify_kwargs["baseline"] = _candidate_baseline(
                            active_journal, codex_baseline
                        )
                        if git.is_git_repo():
                            active_journal = _capture_codex_candidate(
                                git,
                                transaction_store,
                                active_journal,
                                _candidate_baseline(active_journal, codex_baseline),
                                phase=(
                                    "correction-timeout"
                                    if correction_timed_out
                                    else "candidate-captured"
                                ),
                            )
                        active_journal = _capture_evidence(
                            transaction_store,
                            active_journal,
                            log,
                            start_offset=correction_offset,
                            returncode=rc,
                            timed_out=correction_timed_out,
                        )
                        if correction_timed_out:
                            halt = (
                                f"correction worker timed out after {worker_timeout}s"
                            )
                            break
                        # A correction worker that violated the phase contract
                        # cannot hand itself a close; restore the claim so the
                        # fresh verifier still owns the decision.
                        if bd.status(issue_id) != "in_progress":
                            bd.update_status(issue_id, "in_progress")
                        freshness, sync_ms = codegraph_adapter.refresh(
                            target, codegraph_probe
                        )
                        write_log(
                            f"CodeGraph refresh: result={freshness} "
                            f"duration_ms={sync_ms}"
                        )
                        # AC-2: every correction gets its own fresh verifier.
                        verification = _verify_candidate(
                            journal=active_journal,
                            freshness=freshness,
                            sync_ms=sync_ms,
                            **verify_kwargs,
                        )
                        active_journal = verification.journal
                        verification_summary = verification.summary

                    if halt is not None:
                        if escalate:
                            # Exhaustion and unresolved plan gaps leave the issue
                            # human-flagged and OPEN work: no close, no commit.
                            try:
                                bd.add_comment(
                                    issue_id,
                                    "## Ortus correction escalation\n\n"
                                    f"{halt}.\n\n"
                                    f"Candidate: `{active_journal.candidate_hash}`\n"
                                    f"Correction attempts: "
                                    f"{active_journal.corrections}/{max_corrections}\n"
                                    f"Verifier reports: "
                                    f"{', '.join(active_journal.verifier_refs) or 'none'}"
                                    "\n\nThe candidate was NOT committed and the issue "
                                    "was NOT closed. A human decision is required.\n",
                                )
                                bd.add_label(issue_id, "human")
                            except Exception as exc:
                                write_log(f"escalation: bd update failed ({exc})")
                        if halt_phase is not None:
                            active_journal = replace(
                                active_journal, phase=halt_phase
                            )
                            transaction_store.save(active_journal)
                        write_log(
                            f"iter {iters_run}: {halt}; candidate left uncommitted"
                        )
                        output.error(f"grind: {halt}; candidate left uncommitted")
                        break

                    # --- Ortus-owned finalization (AC-4..AC-6) ---------------
                    active_journal, blocker = _finalize_candidate(
                        bd,
                        git,
                        transaction_store,
                        active_journal,
                        repo=target,
                        issue_id=issue_id,
                        integration_branch=integration_branch,
                        baseline=_candidate_baseline(active_journal, codex_baseline),
                        write_log=write_log,
                    )
                    if blocker is not None:
                        write_log(
                            f"iter {iters_run}: finalization blocked — {blocker}"
                        )
                        output.error(
                            f"grind: finalization blocked — {blocker}",
                            hint=(
                                "the transaction journal under logs/ retains the "
                                "recoverable state; re-run grind to resume this "
                                "exact issue"
                            ),
                        )
                        break
                    finalized = True
                    active_journal = None
                    codex_baseline = frozenset()
                    resume_candidate_ready = False
                    # Whatever an earlier worker disowned is still uncommitted in
                    # the tree, so the next iteration inherits it as context it
                    # must leave alone rather than as ownerless drift.
                    handoff = _HandoffState(
                        handoff_paths=disowned_paths,
                        disowned=disowned_paths,
                        active=bool(disowned_paths),
                        notes=(
                            ("work declared unrelated earlier in this run",)
                            if disowned_paths
                            else ()
                        ),
                    )
                    startup_handoff_paths = handoff.handoff_paths
                    recovery_handoff = handoff.active
                    write_log(
                        f"iter {iters_run}: Ortus finalized {issue_id} "
                        "(report, close, commit, sync)"
                    )
                    output.progress("grind", f"finalized {issue_id}")
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

                if delta.closed_one_or_more:
                    tasks_completed += delta.closed_delta
                    write_log(
                        f"iter {iters_run}: closed +{delta.closed_delta} "
                        f"(tasks_completed={tasks_completed}, rc={rc})"
                    )
                    # `finalized` means Ortus already reported, closed, committed
                    # the owned paths, and synchronized. Only the legacy
                    # --condition path, where the worker owns its own close,
                    # still needs the outer commit below.
                    if (
                        not finalized
                        and resolved_backend == "codex"
                        and git.is_git_repo()
                    ):
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
                        # The disowned set belongs in this baseline like every
                        # other ownership check: the paths are still dirty
                        # because grind honored the declaration, and the commit
                        # below takes exactly what this capture calls owned.
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            _candidate_baseline(active_journal, codex_baseline),
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
                    # Either backend: a claim that outlived its worker keeps its
                    # issue association and its edits, so the next grind resumes
                    # the same goal instead of abandoning the work (AC-1).
                    if active_journal is not None and git.is_git_repo():
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            _candidate_baseline(active_journal, codex_baseline),
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
                        active_journal is not None
                        and git.is_git_repo()
                        and active_journal.issue_id in after.in_progress_ids
                    ):
                        active_journal = _capture_codex_candidate(
                            git,
                            transaction_store,
                            active_journal,
                            _candidate_baseline(active_journal, codex_baseline),
                            phase="incomplete-candidate",
                        )
                        if (
                            frozenset(active_journal.candidate_paths)
                            - _TRACKER_EXPORT_PATHS
                        ):
                            write_log(
                                f"iter {iters_run}: preserving incomplete candidate "
                                f"for {active_journal.issue_id}"
                            )
                            output.progress(
                                "grind",
                                f"preserved candidate {active_journal.issue_id}; "
                                "re-run grind to resume",
                            )
                            break
                        # Nothing was produced, so there is no handoff to hold
                        # the queue for; drop the transaction and fall through.
                        transaction_store.clear()
                        active_journal = None
                        write_log(
                            f"iter {iters_run}: WARN no bd-state change and no "
                            f"candidate to preserve (rc={rc})"
                        )
                    else:
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
                    allowed_dirty=codex_baseline | disowned_paths,
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
