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
import re
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, replace
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
from ortus.core.claude import (
    REPO_TOOL_STATE,
    ClaudeRunner,
    ReadOnlyExecutionBlocked,
)
from ortus.core.codegraph import (
    CodeGraphAdapter,
    CodeGraphCapability,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    CodeGraphUnavailable,
    append_normalized,
    parse_transcript,
    phase_contract,
    require_handshake,
)
from ortus.core.attribution import Ownership, describe, path_ownership
from ortus.core.compose import (
    CommitMessage,
    ComposeExceededAuthority,
    ComposeFailed,
    ComposeRejected,
    compose_commit_message,
    guard_read_only,
    shortened,
    validate_message,
    with_default_model,
)
from ortus.core.checks import (
    VERDICT_PASS as MACHINE_PASS,
    CheckRunResult,
    parse_criterion_checks,
    render_tracker_comment,
    run_checks,
)
from ortus.core.config import DEFAULT_MERGE_GATE_TIMEOUT, load_config
from ortus.core.lifecycle import (
    CANDIDATE_CAPTURED,
    CORRECTION_REJECTED,
    CORRECTION_TIMEOUT,
    CORRECTIONS_EXHAUSTED,
    FINALIZATION_BLOCKED,
    FINALIZING,
    HANDOFF,
    IMPLEMENTATION,
    IMPLEMENTATION_REJECTED,
    IMPLEMENTATION_TIMEOUT,
    INCOMPLETE_CANDIDATE,
    ORPHANED_CANDIDATE,
    PLAN_GAP_ESCALATED,
    PLAN_GAP_ROUTED,
    VERIFICATION,
    VERIFICATION_REJECTED,
    VERIFICATION_TIMEOUT,
    VERIFIED_FAIL,
    VERIFIED_PASS,
    finalized_phase,
)
from ortus.core.profiles import AgentProfile, Phase, ProfileError
from ortus.core.readiness import (
    READINESS_MEMORY_KEY,
    ReadinessReport,
    section_text,
)
from ortus.core.transaction import (
    FINALIZATION_STEPS,
    CandidateJournal,
    JournalStore,
)
from ortus.core.transaction import (
    SealedPath,
    candidate_diff,
    contract_packet_changes,
    fingerprint_paths,
    issue_packet_hash,
    moved_sealed_paths,
    restore_sealed_path,
    seal_paths,
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

#: Tracker runtime artifacts that may appear untracked at the repo root
#: (bd >= 1.2 writes `.beads.gate.lock` beside `.beads/`). Never candidate
#: content and never evidence of shared-tree work: one absorbed into a
#: journal's candidate_paths misrouted a branch-scoped resume down the
#: legacy path, whose stale fork point then failed the integration-moved
#: guard on every retry. Ignored via .gitignore too; this set defends repos
#: whose .gitignore predates the entry.
_TRACKER_TOOL_STATE = frozenset({".beads.gate.lock"})


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


def _is_tracker_path(path: str) -> bool:
    """Tracker exports and lock files are never candidate content."""

    return path.split("/", 1)[0] == ".beads" or path in _TRACKER_TOOL_STATE


def _candidate_paths(dirty: frozenset[str], baseline: frozenset[str]) -> frozenset[str]:
    """The dirty paths a verdict is legitimately about."""

    return frozenset(
        path
        for path in dirty - baseline
        if not _is_tool_state(path) and not _is_tracker_path(path)
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
        IMPLEMENTATION_TIMEOUT,
        VERIFICATION_TIMEOUT,
        CORRECTION_TIMEOUT,
        ORPHANED_CANDIDATE,
        INCOMPLETE_CANDIDATE,
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
    # A branch-scoped candidate is everything between the recorded base and
    # the working tree: commits the worker made on its issue branch as well
    # as edits it left uncommitted. The base-relative diff names both; the
    # dirty set still contributes untracked files, which no committed range
    # can name.
    base = journal.base_head if journal.issue_branch else ""
    tip = git.branch_tip(journal.issue_branch) if base else ""
    range_changed: frozenset[str] = frozenset()
    if base and tip and tip != base:
        changed = git.changed_paths(base, tip)
        if changed is None:
            output.error("grind: could not read the candidate's committed range")
            raise typer.Exit(code=1)
        range_changed = changed
    paths = _candidate_paths(dirty | range_changed, baseline)
    try:
        diff = candidate_diff(git.repo, paths, base=base)
    except RuntimeError as exc:
        output.error(f"grind: could not create candidate diff: {exc}")
        raise typer.Exit(code=1) from exc
    digest, diff_ref = store.save_diff(diff)
    updated = journal.with_candidate(
        paths, phase=phase, candidate_hash=digest, diff_ref=diff_ref
    )
    if updated.issue_branch:
        updated = updated.with_branch(updated.issue_branch, git.head_oid())
    store.save(updated)
    return updated


def _candidate_view(
    git: GitClient, journal: CandidateJournal, baseline: frozenset[str]
) -> tuple[frozenset[str], str] | None:
    """The candidate exactly as capture computes it, plus its diff base.

    Dirty paths union the committed range for a branch-scoped journal, minus
    the baseline — every integrity re-check must recompute through this same
    lens, or a worker's legitimate commit reads as the candidate vanishing
    from the worktree. None when the tree or range cannot be read.
    """

    dirty = git.dirty_paths()
    if dirty is None:
        return None
    # The recorded base is meaningful only while the branch it forked still
    # exists; against a deleted branch it would attribute the integration
    # branch's own later commits to the candidate (ortus-ti4i).
    base = (
        journal.base_head
        if journal.issue_branch and git.branch_exists(journal.issue_branch)
        else ""
    )
    # The branch, never the checkout, is the committed range's tip: the
    # primary repository deliberately stays on the integration branch, so an
    # implicit HEAD would read the harness's own landings as the candidate
    # (ortus-bz3c, observed on the first clone-mode resume).
    tip = git.branch_tip(journal.issue_branch) if base else ""
    extra: frozenset[str] = frozenset()
    if base and tip and tip != base:
        changed = git.changed_paths(base, tip)
        if changed is None:
            return None
        extra = changed
    return _candidate_paths(dirty | extra, baseline), base


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

    Nor can a path the same worker went on to edit after disowning it. That
    edit is a deliberate adoption, so its changed regions decide the path:
    wholly this issue's returns it to the candidate, mixed ownership is a plan
    gap for a human, and anything unattributable leaves the declaration alone.
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
    # A declaration is written once, and the same worker may go on to edit the
    # very path it disowned. `_resume_or_handoff` already re-adopts a disowned
    # path whose fingerprint moved, but that runs before a worker starts, so a
    # disown-then-edit inside one session was never re-examined and the edit was
    # dropped from every later candidate. Re-ask the question here, at the only
    # other moment the answer can change.
    readopted, gaps = _reclassify_edited_declarations(repo, journal, honored)
    honored -= set(readopted)
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
    if readopted:
        write_log(
            "handoff: declaration refused; the worker edited this path after "
            "disowning it and every changed region is "
            f"{journal.issue_id}'s, so it returns to the candidate: "
            + ", ".join(readopted[:_HANDOFF_PROMPT_PATHS])
        )
        output.progress(
            "grind",
            f"{len(readopted)} disowned path(s) edited afterwards; re-adopted into "
            "the candidate",
        )
    updated = journal
    if readopted:
        # An earlier declaration in the same run may already hold the path, and
        # `with_unrelated` only ever adds, so dropping it from `honored` is not
        # enough to put it back in the candidate.
        updated = replace(
            updated,
            unrelated_paths=tuple(
                path for path in updated.unrelated_paths if path not in readopted
            ),
        )
    if gaps:
        # Ownership is unresolved, so nothing moves: the path keeps the
        # declaration and a human is told which regions collided. Splitting a
        # file across owners, or guessing which claimant wins, is exactly the
        # improvisation the planning-gap route exists to prevent.
        updated = updated.route_plan_gap()
        for gap in gaps[:_HANDOFF_PROMPT_PATHS]:
            write_log(f"handoff: PLAN-GAP — mixed ownership in a disowned path; {gap}")
        output.progress(
            "grind",
            f"planning gap: {len(gaps)} disowned path(s) carry regions owned by more "
            "than one issue",
        )
    if honored:
        updated = updated.with_unrelated(honored)
    if updated is journal:
        return journal
    store.save(updated)
    if honored:
        write_log(
            "handoff: worker declared unrelated; left untouched and never committed: "
            + ", ".join(sorted(honored))
        )
        output.progress(
            "grind",
            f"{len(honored)} inherited path(s) declared unrelated; left untouched",
        )
    return updated


def _reclassify_edited_declarations(
    repo: Path, journal: CandidateJournal, honored: set[str]
) -> tuple[list[str], list[str]]:
    """Split declared paths the worker then edited into re-adopted and blocked.

    A declared path whose content still matches the fingerprint recorded at
    handoff is nobody's work and keeps today's behavior exactly. One whose
    content moved was picked back up deliberately, so its changed regions decide
    it: all of them the claimed issue's re-adopts the whole path, a mix of
    owners is a planning gap, and anything else — including a region nothing in
    the index or the work spec can name — leaves the declaration standing.
    """

    # f2he.3: ownership is not decided by path fingerprints. A declaration
    # stands; .beads/ exclusion handles intake.
    del repo, journal, honored
    return [], []


def _concrete_locations(repo: Path, journal: CandidateJournal) -> str:
    """The claimed issue's Concrete locations, read from the frozen work spec.

    The work-spec artifact is the authoritative copy this attempt is bound to, so
    ownership is judged against the same text the verifier will read rather
    than against whatever bd holds now. An unreadable or unauthored section
    names nothing, which makes every region foreign and honors the declaration.
    """

    if not journal.issue_packet_ref:
        return ""
    try:
        packet = json.loads((repo / journal.issue_packet_ref).read_bytes())
    except (OSError, ValueError):
        return ""
    if not isinstance(packet, dict):
        return ""
    return section_text(packet.get("design"), "Concrete locations")


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
            + ". Assess every change against the work spec: keep and finish what "
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
    paths = frozenset(
        path
        for path in dirty
        if not _is_tool_state(path) and not _is_tracker_path(path)
    )
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
        paths, phase=HANDOFF, candidate_hash=digest, diff_ref=diff_ref
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
            inherited = frozenset(
                path
                for path in dirty
                if not _is_tool_state(path) and not _is_tracker_path(path)
            )
        claimed = bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS)
        if not inherited:
            # A leftover claim is the next window's goal even when the tree is
            # clean.
            if len(claimed) == 1:
                issue_id = next(iter(claimed))
                write_log(
                    "transaction handoff: resuming the single claimed issue "
                    f"{issue_id}"
                )
                return _HandoffState(resume_issue_id=issue_id, notes=notes)
            return _HandoffState(notes=notes)
        state = _HandoffState(handoff_paths=inherited, active=True, notes=notes)
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
    # f2he.3: do not re-adopt unrelated paths from fingerprint drift.
    # What the prior worker on this issue actually owned, read before the live
    # candidate is recomputed below. Only this recorded set is attributable: the
    # recomputed one sweeps in whatever else went dirty since — a stranded file
    # from another issue among it — and that must stay disownable. A rebuilt
    # journal inferred its candidate from the worktree rather than recording it,
    # so it attributes nothing.
    recorded_candidate = frozenset() if rebuilt else frozenset(journal.candidate_paths)
    baseline = _candidate_baseline(journal, frozenset())
    # The same lens every other integrity site uses: a branch-scoped
    # candidate is the committed range plus the worktree, measured against
    # the recorded base. The worktree-only view read a committed candidate
    # as empty here and "refreshed" the journal to nothing (ortus-4fxr).
    diff_base = ""
    diff_tip = ""
    if git.is_git_repo():
        view = _candidate_view(git, journal, baseline)
        if view is None:
            moved.append("the candidate's committed range could not be read")
            candidate = _candidate_paths(dirty, baseline)
        else:
            candidate, diff_base = view
            if diff_base and git.current_branch() != journal.issue_branch:
                # The primary is parked on the integration branch; the
                # candidate's tree is its branch ref, not this checkout
                # (ortus-bz3c). A legacy resume sitting on the branch keeps
                # the worktree reading for its dirty tail.
                diff_tip = git.branch_tip(journal.issue_branch)
    else:
        candidate = _candidate_paths(dirty, baseline)
    if prior_phase in _SEALED_PHASES and candidate != frozenset(journal.candidate_paths):
        moved.append("the candidate path set changed since the prior worker")
    try:
        diff = (
            candidate_diff(repo, candidate, base=diff_base, tip=diff_tip)
            if git.is_git_repo()
            else b""
        )
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
        # A branch-scoped journal's base is the fork point the keystone
        # recorded at claim; after a mid-run failure the tree sits on the
        # issue branch, so "current head" is the branch tip — recording it
        # as the base poisoned the integration-moved guard (ortus-4fxr).
        base_head=(journal.base_head if journal.issue_branch else current_head),
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
        # A captured-but-empty candidate has nothing for a verifier to
        # judge; resuming it to verification traps the transaction
        # (ortus-ti4i). It re-implements instead.
        candidate_ready=bool(candidate)
        and prior_phase in {CANDIDATE_CAPTURED, VERIFICATION, VERIFICATION_TIMEOUT},
        active=True,
        prior_phase=prior_phase,
        diff_ref=journal.candidate_diff_ref,
        notes=tuple(moved),
    )


def _packet_artifact_intact(repo: Path, journal: CandidateJournal) -> bool:
    """Rehash the on-disk work spec the verifier is about to be handed.

    The work-spec reference is a plain file under ``logs/``, so a worker that
    ignores the phase contract could rewrite it and verify itself against a
    work spec nobody authorized. bd being unchanged is not enough — the artifact
    bytes must still hash to the advertised digest.
    """

    if not journal.issue_packet_ref or not journal.issue_packet_hash:
        return True
    try:
        payload = (repo / journal.issue_packet_ref).read_bytes()
    except OSError:
        return False
    return sha256_bytes(payload) == journal.issue_packet_hash


def _packet_drift(
    repo: Path, journal: CandidateJournal, current: dict[str, Any]
) -> str:
    """Name the contract fields that moved since this work spec was frozen.

    The frozen work spec is on disk as the very artifact the verifier is handed, so
    the *before* values are readable even though bd only ever reports the after.
    """

    stored: Any = None
    if journal.issue_packet_ref:
        try:
            stored = json.loads((repo / journal.issue_packet_ref).read_bytes())
        except (OSError, ValueError):
            stored = None
    if not isinstance(stored, dict):
        return "the frozen work-spec artifact is unreadable, so the fields cannot be named"
    changes = contract_packet_changes(stored, current)
    return "; ".join(changes) if changes else "no contract field differs"


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
        "Independently inspect the exact work spec and candidate diff at the paths "
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
        f"Work spec: {journal.issue_packet_ref}\n"
        f"Candidate diff: {journal.candidate_diff_ref}\n"
        f"Captured evidence: {json.dumps(journal.evidence, ensure_ascii=False)}\n\n"
        "The candidate is everything between the recorded base commit and the "
        "working tree — commits the worker made on its issue branch as well as "
        "any edits it left uncommitted — and the diff artifact above carries "
        "exactly that. The branch, not the worktree alone, is the subject.\n\n"
        "The JSON object must have exactly these fields: schema (1), candidate_hash, "
        "decision (pass or fail), criteria (non-empty array of objects with exactly id, "
        "status, evidence), and non-empty string arrays commands, reviewed_files, "
        "reviewed_interfaces, risks, findings, codegraph. A pass requires every criterion "
        "to pass; a fail requires at least one failed criterion. Bind candidate_hash to "
        "the supplied SHA-256 exactly.\n\n"
        "Criterion ids must be exactly the AC-N identifiers listed in the work spec's "
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
        "Judge whether the candidate is correct; leave how fast a test is to CI.\n\n"
        "Build any throwaway comparison tree — HEAD without the candidate, say — "
        "with `git archive <ref> | tar -x -C \"$TMPDIR/tree\"` (it takes any ref, "
        "and pathspecs narrow it to the paths you need; always extract outside "
        "the repository so the snapshot never overwrites the candidate), or with "
        "`git clone --shared` when the tree "
        "must build or needs git metadata; this repository's version derives from "
        "vcs metadata, so an archive extraction cannot even install. Never run "
        "`git worktree add`: the sandbox's read-only bind mounts make the "
        "registration unremovable, so cleanup fails with Device or resource busy "
        "and every later session pays for the leaked entry. A shared clone is a "
        "plain directory, not a worktree, and ordinary removal deletes it.\n"
        + probe_text
    )


# ---------------------------------------------------------------------------
# Machine verification (Phase L1 wiring)
# ---------------------------------------------------------------------------

#: Header of the worker's per-criterion claims block in the completion
#: comment. Version-pinned like the CodeGraph block: a future schema is a
#: missing block to this parser, never a misread one.
_CLAIMS_HEADER = "**Claims v1**"
_CLAIM_LINE = re.compile(r"^(AC-\d+)\s*:\s*(pass|fail)\b", re.IGNORECASE)

#: Seam for the AC runner, so a test can script pipeline results the way it
#: scripts worker behavior — the wiring under test stays the real one.
_run_machine_checks = run_checks


def _acceptance_hash(acceptance_criteria: object) -> str:
    """Hash of one acceptance_criteria field: the identity judgment binds to."""

    return sha256_bytes(str(acceptance_criteria or "").encode("utf-8", "replace"))


def _claim_time_criteria(repo: Path, journal: CandidateJournal) -> str | None:
    """The acceptance_criteria the work spec carried at claim, or None.

    Read from the frozen claim-time artifact, not from bd: the pipeline runs
    the commands that were hashed at claim, so an edit landing mid-run can
    change what the next claim judges but never what this one is judging.
    """

    if not journal.issue_packet_ref:
        return None
    try:
        payload = json.loads(
            (repo / journal.issue_packet_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return str(payload.get("acceptance_criteria") or "")


def _parse_claims(comment: str) -> dict[str, str] | None:
    """Per-criterion claims from one comment, or None when it carries no block.

    None and {} differ on purpose: a comment without the header never claimed
    anything, while a header with no parseable lines claimed and said nothing —
    both fail the claim diff, with different messages.
    """

    if _CLAIMS_HEADER not in comment:
        return None
    claims: dict[str, str] = {}
    for line in _block_lines(comment, _CLAIMS_HEADER):
        stripped = line.strip().lstrip("-*+ ").strip()
        match = _CLAIM_LINE.match(stripped)
        if match:
            claims[match.group(1).upper()] = match.group(2).lower()
    return claims


def _latest_claims(bd: BdClient, issue_id: str) -> dict[str, str] | None:
    """The newest comment's claims block. Each round supersedes the last."""

    for body in reversed(_issue_comments(bd, issue_id)):
        claims = _parse_claims(body)
        if claims is not None:
            return claims
    return None


def _claim_disagreements(
    claims: dict[str, str] | None, run: CheckRunResult
) -> tuple[str, ...]:
    """Where the worker's word departs from the measured results, per criterion.

    Any disagreement fails the run — in either direction, so a claim can never
    stand in for a result and lying is strictly worse than silence. A missing
    block is one failure naming the block; silence about one criterion is a
    failure naming that criterion.
    """

    if claims is None:
        return (
            f"the completion comment carries no {_CLAIMS_HEADER} block; "
            "every criterion is unclaimed",
        )
    problems: list[str] = []
    measured = {record.criterion_id: record for record in run.results}
    for criterion_id, record in measured.items():
        claimed = claims.get(criterion_id)
        actual = "pass" if record.verdict == MACHINE_PASS else "fail"
        if claimed is None:
            problems.append(
                f"{criterion_id}: unclaimed; the pipeline measured {record.verdict}"
            )
        elif claimed != actual:
            problems.append(
                f"{criterion_id}: claimed {claimed}, measured {record.verdict} "
                f"(`{record.command}`)"
            )
    problems.extend(
        f"{criterion_id}: claimed but not among the work spec's criterion checks"
        for criterion_id in sorted(set(claims) - set(measured))
    )
    return tuple(problems)


def _criterion_evidence(record: Any) -> str:
    """One criterion's measured outcome as report evidence."""

    exit_text = (
        "no exit code" if record.exit_code is None else f"exit {record.exit_code}"
    )
    if record.kind is None:
        return (
            f"{record.verdict} — {exit_text} in {record.duration_seconds:.1f}s "
            f"— `{record.command}`"
        )
    base_text = (
        "no exit code"
        if record.base_exit_code is None
        else f"exit {record.base_exit_code}"
    )
    return (
        f"{record.verdict} ({record.kind}) — base {base_text}, branch {exit_text} "
        f"in {record.duration_seconds:.1f}s — `{record.command}`"
    )


def _failing_finding(record: Any) -> str:
    """A failed criterion as a correction-ready finding: command, then output.

    The command leads so it survives the correction packet's per-entry bound,
    and the output keeps its tail — a test run announces its failure at the
    bottom.
    """

    detail = " ".join(record.output.split())
    if len(detail) > 300:
        detail = "…" + detail[-300:]
    return (
        f"{record.criterion_id} {record.verdict}: `{record.command}` — "
        + (detail or "no output")
    )


def _machine_verdict(
    journal: CandidateJournal,
    run: CheckRunResult,
    disagreements: tuple[str, ...],
) -> Verdict:
    """Fold one pipeline run and its claim diff into the verdict shape.

    The retry controller, the correction packet, and finalization all consume
    `Verdict`, so the machine pipeline speaks it too — one verdict grammar,
    two producers.
    """

    criteria = tuple(
        {
            "id": record.criterion_id,
            "status": "pass" if record.verdict == MACHINE_PASS else "fail",
            "evidence": _criterion_evidence(record),
        }
        for record in run.results
    )
    findings: list[str] = []
    if run.environment is not None:
        findings.append(run.environment.reason)
    findings.extend(
        f"work spec: {failure.message}" for failure in run.packet_failures
    )
    if not run.results and run.environment is None and not run.packet_failures:
        findings.append(
            "no criterion checks parsed from the work spec — nothing verified"
        )
    findings.extend(
        _failing_finding(record)
        for record in run.results
        if record.verdict != MACHINE_PASS
    )
    findings.extend(
        f"claims disagree with results — {item}" for item in disagreements
    )
    passed = run.ok and not disagreements
    return Verdict(
        candidate_hash=journal.candidate_hash,
        decision="pass" if passed else "fail",
        criteria=criteria,
        commands=tuple(record.command for record in run.results),
        reviewed_files=tuple(journal.candidate_paths),
        reviewed_interfaces=(),
        risks=(),
        findings=tuple(findings),
        codegraph=(),
    )


def _machine_report(
    journal: CandidateJournal,
    issue_id: str,
    run: CheckRunResult,
    disagreements: tuple[str, ...],
    verdict: Verdict,
) -> str:
    """The durable verification comment: the runner's own record, then claims.

    Commands, verdicts, and exit codes come verbatim from the runner's
    rendering — the record the tracker keeps, minus the agent that used to
    type it.
    """

    lines = [
        "## Ortus machine verification record",
        "",
        f"Issue: {issue_id}",
        f"Decision: **{verdict.decision.upper()}**",
    ]
    if journal.base_head:
        lines.append(f"Base commit: `{journal.base_head}`")
    lines.append(f"Verifier attempt: {journal.attempt}")
    lines.extend(["", render_tracker_comment(run).rstrip(), "", "### Claims"])
    if disagreements:
        lines.extend(f"- {item}" for item in disagreements)
    else:
        lines.append("- the worker's claims agree with the measured results")
    return bound_report("\n".join(lines) + "\n")


def _machine_verify_candidate(
    *,
    bd: BdClient,
    git: GitClient,
    store: JournalStore,
    journal: CandidateJournal,
    repo: Path,
    log: Path,
    write_log: Callable[[str], None],
    issue_id: str,
    probe: CodeGraphProbe,
    baseline: frozenset[str],
    freshness: str,
    sync_ms: int,
    iteration: int,
    integration_branch: str,
    worker_timeout: int = 0,
) -> _VerificationResult:
    """Judge the candidate with the deterministic pipeline: no agent, no tokens.

    The subject is the committed issue branch: the AC runner executes the
    claim-time criterion checks in disposable clones (red–green for tagged
    criteria), and the worker's per-criterion claims are diffed against the
    measured results. Nothing here executes in the working tree, so there is
    no seal and no mutation guard — the pipeline cannot touch what it judges.
    """

    journal = journal.begin_verification()
    store.save(journal)
    verify_started = time.monotonic()
    expected_criteria: dict[str, None] = {}

    def _summarize() -> Any:
        # No agent ran in this phase; parsing the empty tail of the log keeps
        # the summary interface the rest of the loop expects, with no events.
        summary = parse_transcript(
            log,
            phase=CodeGraphPhase.VERIFICATION,
            probe=probe,
            start_offset=log.stat().st_size if log.exists() else 0,
        )
        summary.freshness = freshness
        summary.sync_duration_ms = sync_ms
        return summary

    def _reject(reason: str, *, spec_defect: bool = False) -> _VerificationResult:
        nonlocal journal
        if bd.show(issue_id).get("status") != "in_progress":
            bd.update_status(issue_id, "in_progress")
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
        report_ref = store.save_report(
            journal.candidate_hash, report, attempt=journal.attempt
        )
        bd.add_comment(issue_id, report)
        journal = journal.finish_verification(
            report_ref, phase=VERIFICATION_REJECTED
        )
        store.save(journal)
        _append_verdict_event(
            log,
            decision="rejected",
            candidate_hash=journal.candidate_hash,
            reason=reason,
        )
        write_log(f"iter {iteration}: machine verification rejected: {reason}")
        return _VerificationResult(
            journal=journal,
            summary=_summarize(),
            failure=reason,
            spec_defect=spec_defect,
        )

    claim_criteria = _claim_time_criteria(store.repo, journal)
    if claim_criteria is None:
        return _reject(
            "the claim-time work-spec artifact is unreadable; the pipeline "
            "judges only criteria whose claim-time identity it can prove",
            spec_defect=True,
        )
    expected_criteria = dict.fromkeys(re.findall(r"\bAC-\d+\b", claim_criteria))
    current_packet = bd.show(issue_id)
    # Judgment is bound to the criteria hashed at claim. An edit landing after
    # claim is a blocker to resolve, never a silent re-read.
    if _acceptance_hash(current_packet.get("acceptance_criteria")) != (
        _acceptance_hash(claim_criteria)
    ):
        return _reject(
            "the acceptance criteria changed after claim; verification judges "
            "the claim-time criteria — relabel the issue for a fresh claim "
            "under the edited work spec",
            spec_defect=True,
        )
    if issue_packet_hash(current_packet) != journal.issue_packet_hash:
        return _reject(
            "authoritative work spec changed during verification — "
            + _packet_drift(store.repo, journal, current_packet),
            spec_defect=True,
        )
    # An unparseable criterion check indicts the packet, not the candidate: a
    # correction worker could only satisfy it by editing the claimed spec,
    # which the acceptance-hash guard above forbids. Reject before spending a
    # single check run, and mark it a spec defect so the retry controller
    # escalates to the operator instead of resuming into the same wall.
    _, packet_failures = parse_criterion_checks(claim_criteria)
    if packet_failures:
        return _reject(
            "the work spec's criterion checks cannot be parsed: "
            + "; ".join(failure.message for failure in packet_failures)
            + ". No candidate correction can repair the packet — repair the "
            "work spec, then relabel the issue for a fresh claim under the "
            "repaired criteria",
            spec_defect=True,
        )
    if not git.branch_exists(journal.issue_branch):
        return _reject(
            f"the issue branch {journal.issue_branch} no longer exists"
        )
    # A disown can arrive after a capture commit already swept the path in —
    # a correction worker recognizing inherited work as somebody else's. The
    # capture commit is the harness's own, so the harness takes it back: a
    # soft reset returns its paths to the staged state they were captured
    # from, and the re-capture below excludes what is now disowned.
    capture_subject = f"{issue_id}: capture uncommitted candidate work"
    while journal.unrelated_paths and git.current_branch() == journal.issue_branch:
        if (git.head_message() or "").partition("\n")[0] != capture_subject:
            break
        touched = git.changed_paths(f"{git.head_oid()}~1")
        if touched is None or not (touched & frozenset(journal.unrelated_paths)):
            break
        if not git.reset_soft(f"{git.head_oid()}~1"):
            return _reject(
                "could not unwind a capture commit carrying disowned paths"
            )
        journal = journal.with_branch(journal.issue_branch, git.head_oid())
        store.save(journal)
        write_log(
            f"iter {iteration}: unwound a capture commit to honor a disown of "
            + ", ".join(sorted(touched & frozenset(journal.unrelated_paths)))
        )
    dirty = git.dirty_paths()
    if dirty is None:
        return _reject("could not inspect the working tree before judgment")
    uncommitted = sorted(dirty & frozenset(journal.candidate_paths))
    if uncommitted:
        # The pipeline judges refs, so the harness completes the commit
        # obligation a worker left unmet — a Codex sandbox cannot commit at
        # all. The capture message is deliberately below the message contract:
        # finalization's compose pass replaces it with the durable one.
        if git.current_branch() != journal.issue_branch:
            return _reject(
                f"uncommitted candidate paths exist but the tree is on "
                f"{git.current_branch() or 'a detached HEAD'}, not "
                f"{journal.issue_branch}"
            )
        committed = git.commit_paths(
            frozenset(uncommitted),
            f"{issue_id}: capture uncommitted candidate work\n\n"
            "Pre-judgment capture of the paths the worker left uncommitted.\n\n"
            "The finalization pass composes the durable message.",
        )
        if not committed:
            return _reject(
                "could not commit uncommitted candidate paths for judgment: "
                + committed.reason
            )
        journal = journal.with_branch(journal.issue_branch, git.head_oid())
        # Recapture the candidate identity: the same content hashes
        # differently once tracked (an untracked file enters the bundle as a
        # patch against /dev/null), and the recorded hash must describe the
        # committed form everything downstream re-checks.
        view = _candidate_view(git, journal, baseline)
        if view is None:
            return _reject(
                "could not re-inspect the candidate after the capture commit"
            )
        paths, base = view
        try:
            digest, diff_ref = store.save_diff(
                candidate_diff(repo, paths, base=base)
            )
        except RuntimeError as exc:
            return _reject(
                f"could not re-hash the candidate after the capture commit: {exc}"
            )
        journal = journal.with_candidate(
            paths,
            phase=VERIFICATION,
            candidate_hash=digest,
            diff_ref=diff_ref,
        )
        store.save(journal)
        write_log(
            f"iter {iteration}: captured {len(uncommitted)} uncommitted "
            f"candidate path(s) onto {journal.issue_branch} for judgment"
        )
    ref = git.branch_tip(journal.issue_branch) or journal.issue_branch
    write_log(
        f"iter {iteration}: machine checks running against "
        f"{journal.issue_branch} at {ref}"
    )
    run = _run_machine_checks(
        repo,
        claim_criteria,
        str(ref),
        base_ref=(journal.base_head or integration_branch),
        # A criterion that runs the full gate needs gate-length time; the
        # per-command bound follows the worker budget rather than the
        # runner's 10-minute default (two live criteria timed out there).
        timeout_seconds=float(
            max(1800, worker_timeout if worker_timeout > 0 else 0)
        ),
    )
    disagreements = _claim_disagreements(_latest_claims(bd, issue_id), run)
    verdict = _machine_verdict(journal, run, disagreements)
    report = _machine_report(journal, issue_id, run, disagreements, verdict)
    report_ref = store.save_report(
        journal.candidate_hash, report, attempt=journal.attempt
    )
    bd.add_comment(issue_id, report)
    journal = journal.finish_verification(
        report_ref, phase=(VERIFIED_PASS if verdict.passed else VERIFIED_FAIL)
    )
    store.save(journal)
    _append_verdict_event(
        log, decision=verdict.decision, candidate_hash=journal.candidate_hash
    )
    write_log(
        f"iter {iteration}: verifier verdict={verdict.decision} "
        f"candidate={journal.candidate_hash}"
    )
    passed_count = sum(1 for r in run.results if r.verdict == MACHINE_PASS)
    claims_word = "disagree" if disagreements else "agree"
    output.progress(
        "grind",
        f"verdict: {verdict.decision.upper()} — machine checks passed "
        f"{passed_count}/{len(run.results)} criteria, claims {claims_word} "
        f"(candidate {journal.candidate_hash[:12]}) after "
        f"{_fmt_duration(time.monotonic() - verify_started)}",
    )
    return _VerificationResult(
        journal=journal, summary=_summarize(), verdict=verdict
    )


def _verification_pass(
    *,
    reviewer_enabled: bool,
    integration_branch: str,
    **kwargs: Any,
) -> _VerificationResult:
    """Dispatch one verification round: machine pipeline, then reviewer by flag.

    The machine pipeline is the default judgment. The agent reviewer is a
    configuration-selected step that runs only after a green machine run —
    review is policy, not architecture, and a red machine run never spends
    reviewer tokens. A journal with no issue branch predates branch-scoped
    candidates; the agent verifier remains that legacy candidate's judge.
    """

    journal: CandidateJournal = kwargs["journal"]
    write_log: Callable[[str], None] = kwargs["write_log"]
    iteration: int = kwargs["iteration"]
    if not journal.issue_branch:
        write_log(
            f"iter {iteration}: journal has no issue branch; the agent "
            "verifier judges this legacy candidate"
        )
        return _verify_candidate(**kwargs)
    machine_result = _machine_verify_candidate(
        bd=kwargs["bd"],
        git=kwargs["git"],
        store=kwargs["store"],
        journal=journal,
        repo=kwargs["repo"],
        log=kwargs["log"],
        write_log=write_log,
        issue_id=kwargs["issue_id"],
        probe=kwargs["probe"],
        baseline=kwargs["baseline"],
        freshness=kwargs["freshness"],
        sync_ms=kwargs["sync_ms"],
        iteration=iteration,
        integration_branch=integration_branch,
        worker_timeout=int(kwargs.get("worker_timeout") or 0),
    )
    if not reviewer_enabled:
        return machine_result
    if not machine_result.passed:
        write_log(
            f"iter {iteration}: reviewer skipped — the machine pipeline is "
            "red, so no reviewer tokens are spent"
        )
        return machine_result
    write_log(
        f"iter {iteration}: machine pipeline green; reviewer flag on — "
        "dispatching the agent reviewer"
    )
    # The reviewer's prompt references artifacts by repo-relative path; in a
    # worker workspace those live primary-side, so copies are staged in.
    store: JournalStore = kwargs["store"]
    review_repo = Path(kwargs["repo"])
    if review_repo.resolve() != store.repo.resolve():
        for ref in (
            machine_result.journal.issue_packet_ref,
            machine_result.journal.candidate_diff_ref,
        ):
            if not ref:
                continue
            try:
                staged = review_repo / ref
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes((store.repo / ref).read_bytes())
            except OSError as exc:
                write_log(
                    f"iter {iteration}: could not stage {ref} for the "
                    f"reviewer ({exc})"
                )
    return _verify_candidate(**{**kwargs, "journal": machine_result.journal})


def _transcript_session_id(log: Path, *, start_offset: int) -> str:
    """The last session id a transcript segment carries, or empty.

    This is what lets a correction return to the session that produced the
    candidate instead of a fresh context that has never seen its own attempt.
    """

    session_id = ""
    try:
        with log.open("rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                try:
                    event = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict):
                    value = event.get("session_id")
                    if isinstance(value, str) and value:
                        session_id = value
    except OSError:
        return ""
    return session_id


def _declared_reviewed(verdict: Verdict | None, repo: Path) -> frozenset[str]:
    """The candidate paths a verdict claims its author reviewed.

    Normalized to the repo-relative spelling the candidate path set uses, so a
    verifier that wrote `./src/app.ts` or an absolute path is still understood
    to have named that path.
    """

    if verdict is None:
        return frozenset()
    prefixes = tuple(
        f"{base}/" for base in {repo.as_posix(), repo.resolve().as_posix()}
    )
    declared: set[str] = set()
    for entry in verdict.reviewed_files:
        text = str(entry).strip().replace("\\", "/")
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix) :]
        while text.startswith("./"):
            text = text[2:]
        text = text.rstrip("/")
        if text:
            declared.add(text)
    return frozenset(declared)


def _restore_rebuilt_candidate(
    repo: Path,
    *,
    sealed: dict[str, SealedPath],
    reviewed: frozenset[str],
    write_log: Callable[[str], None],
    iteration: int,
) -> tuple[str, ...]:
    """Put back candidate paths the reviewer's own checks rebuilt.

    A read-only reviewer must not change what it is judging, so a path it
    declared reviewing that moved anyway is misconduct and stays fatal. But the
    work spec also tells that reviewer to run the project's checks, and a
    repository that commits build output — a bundled image, a transpiled
    sibling of a TypeScript source, a lockfile a dependency install rewrites —
    has those checks rewrite candidate paths nobody edited. Ortus cannot see
    inside a subprocess and will not guess which paths a project generates, so
    attribution keys on the verdict the reviewer signed: a path it never claims
    to have opened, that moved while its commands ran, is a rebuild.

    The rebuilt bytes are never adopted. They are replaced by the sealed ones,
    so what finalization commits is the candidate that was actually reviewed,
    and every restored path is named, because an artifact that differs on every
    run is a real finding about the repository even when it is nobody's fault.

    Returns the restored paths. Raises `VerdictError` for a reviewed path that
    moved and for a path that could not be put back.
    """

    moved = moved_sealed_paths(repo, sealed)
    edited = tuple(path for path in moved if path in reviewed)
    if edited:
        write_log(
            f"iter {iteration}: verifier changed candidate paths it reviewed: "
            + ", ".join(edited)
        )
        raise VerdictError("verifier mutated the candidate during read-only review")
    restored: list[str] = []
    failures: list[str] = []
    for path in moved:
        try:
            restore_sealed_path(repo, path, sealed[path])
        except OSError as exc:
            failures.append(f"{path} ({exc})")
        else:
            restored.append(path)
    for path in restored:
        write_log(
            f"iter {iteration}: restored {path} — the verifier's checks rebuilt it "
            "during read-only review; the sealed candidate is what gets committed"
        )
    if failures:
        raise VerdictError(
            "could not restore the candidate after read-only review: "
            + "; ".join(failures)
        )
    return tuple(restored)


@dataclass
class _VerificationResult:
    """What one fresh verifier attempt produced, for the retry controller."""

    journal: CandidateJournal
    summary: Any
    verdict: Verdict | None = None
    failure: str | None = None
    timed_out: bool = False
    #: True when the failure indicts frozen claim-time state — unparseable
    #: criterion checks, a packet edited after claim, an unreadable claim
    #: artifact. No candidate correction and no resume can change that state,
    #: so the retry controller must escalate to the operator instead of
    #: halting for a resume that would fail identically (ortus-lwr9).
    spec_defect: bool = False

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
    # Sealed before anything the reviewer can run, so a candidate path its
    # checks rebuild can be put back to what it judged rather than reported as
    # tampering (ortus-9yh9).
    sealed = seal_paths(repo, journal.candidate_paths) if git.is_git_repo() else {}
    if configure_codegraph is not None:
        configure_codegraph(probe.capability)
    verification_probe = probe
    if configure_codegraph is not None:
        configure_codegraph(verification_probe.capability)
    verifier_prompt = _verifier_prompt(
        journal, phase_contract(CodeGraphPhase.VERIFICATION, verification_probe)
    )
    verify_offset = log.stat().st_size if log.exists() else 0
    if probe.available:
        write_log("verification CodeGraph handshake requested")
    else:
        output.progress("grind", "verification CodeGraph handshake fallback active")
    verify_started = time.monotonic()
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
        timeout_phase = VERIFICATION_TIMEOUT
        if git.is_git_repo():
            view = _candidate_view(git, journal, baseline)
            if view is None:
                timeout_failure += "; could not inspect the candidate after timeout"
                timeout_phase = VERIFICATION_REJECTED
            else:
                post_paths, post_base = view
                if post_paths != frozenset(journal.candidate_paths):
                    timeout_failure += "; verifier mutated the candidate path set"
                    timeout_phase = VERIFICATION_REJECTED
                else:
                    try:
                        post_diff = candidate_diff(
                            repo, post_paths, base=post_base
                        )
                        if sha256_bytes(post_diff) != journal.candidate_hash:
                            # No verdict exists to attribute the change to, and
                            # the run is rejected either way; restoring is what
                            # leaves the preserved candidate identical to the
                            # one the journal records, so a resume can use it.
                            restored = _restore_rebuilt_candidate(
                                repo,
                                sealed=sealed,
                                reviewed=frozenset(),
                                write_log=write_log,
                                iteration=iteration,
                            )
                            if (
                                sha256_bytes(
                                    candidate_diff(
                                        repo, post_paths, base=post_base
                                    )
                                )
                                != journal.candidate_hash
                            ):
                                timeout_failure += "; verifier mutated the candidate"
                                timeout_phase = VERIFICATION_REJECTED
                            elif restored:
                                timeout_failure += (
                                    "; the verifier's checks rebuilt "
                                    + ", ".join(restored)
                                    + ", restored from the seal"
                                )
                    except RuntimeError as exc:
                        timeout_failure += f"; could not hash the candidate: {exc}"
                        timeout_phase = VERIFICATION_REJECTED
                    except VerdictError as exc:
                        timeout_failure += f"; {exc}"
                        timeout_phase = VERIFICATION_REJECTED
        current_packet = bd.show(issue_id)
        if issue_packet_hash(current_packet) != journal.issue_packet_hash:
            timeout_failure += (
                "; authoritative work spec changed during verification — "
                + _packet_drift(store.repo, journal, current_packet)
            )
            timeout_phase = VERIFICATION_REJECTED
            if current_packet.get("status") != "in_progress":
                bd.update_status(issue_id, "in_progress")
        result = _reject(timeout_failure, phase=timeout_phase, summary=_summarize())
        write_log(
            f"iter {iteration}: preserved verifier-timeout candidate for "
            f"{result.journal.issue_id}: {list(result.journal.candidate_paths)}"
        )
        # Preservation is part of the retry controller's one terminal
        # narrative for this failure; no separate console line here.
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
        write_log("verification CodeGraph handshake succeeded")
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
            view = _candidate_view(git, journal, baseline)
            if view is None:
                raise VerdictError(
                    "could not inspect the candidate after verification"
                )
            post_paths, post_base = view
            if post_paths != frozenset(journal.candidate_paths):
                raise VerdictError(
                    "verifier mutated the candidate path set during read-only review"
                )
            post_diff = candidate_diff(repo, post_paths, base=post_base)
            if sha256_bytes(post_diff) != journal.candidate_hash:
                restored = _restore_rebuilt_candidate(
                    repo,
                    sealed=sealed,
                    reviewed=_declared_reviewed(verdict, repo),
                    write_log=write_log,
                    iteration=iteration,
                )
                if (
                    sha256_bytes(candidate_diff(repo, post_paths, base=post_base))
                    != journal.candidate_hash
                ):
                    # Something moved that the seal does not cover, so the
                    # sealed candidate is no longer what is on disk.
                    raise VerdictError(
                        "verifier mutated the candidate during read-only review"
                    )
                output.progress(
                    "grind",
                    "restored candidate paths the verifier's checks rebuilt: "
                    + ", ".join(restored),
                )
        current_packet = bd.show(issue_id)
        if issue_packet_hash(current_packet) != journal.issue_packet_hash:
            raise VerdictError(
                "authoritative work spec changed during verification — "
                + _packet_drift(store.repo, journal, current_packet)
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
        result = _reject(failure, phase=VERIFICATION_REJECTED, summary=summary)
        write_log(f"iter {iteration}: verifier rejected: {failure}")
        # The retry controller owns the one console narrative for this
        # rejection; a second line here would double-print the failure.
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
        report_ref, phase=(VERIFIED_PASS if verdict.passed else VERIFIED_FAIL)
    )
    store.save(journal)
    _append_verdict_event(
        log, decision=verdict.decision, candidate_hash=journal.candidate_hash
    )
    write_log(
        f"iter {iteration}: verifier verdict={verdict.decision} "
        f"candidate={journal.candidate_hash}"
    )
    output.progress(
        "grind",
        f"verdict: {verdict.decision.upper()} "
        f"(candidate {journal.candidate_hash[:12]}) after "
        f"{_fmt_duration(time.monotonic() - verify_started)}",
    )
    return _VerificationResult(journal=journal, summary=summary, verdict=verdict)


_PLAN_GAP_MARKER = re.compile(r"(?i)plan[\s_-]?gap")
_CORRECTION_ENTRY_CHARS = 400
_CORRECTION_MAX_FINDINGS = 6

#: The rules `validate_message` enforces on a worker's own commit message,
#: stated where the writer writes. The first two autonomous landings both had
#: their messages rejected for breaking rules the contract never stated, so
#: every writer-facing contract carries this same rule set;
#: tests/test_grind_prompt_content.py pins it against the validator's.
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
    "never narration of how the commit was produced (attempt counts, verifier "
    "verdicts, phase names, candidate hashes)."
)

#: The implementation phase rules injected ahead of the worker's condition.
#: Module-level so the prompt-content tests can hold its message guidance to
#: the same rule set the finalization gate enforces.
_IMPLEMENTATION_INSTRUCTION = (
    "Follow the one-issue goal-prompt loop. Session-close that id per "
    "AGENTS.md. " + _MESSAGE_RULES + " Do not pick a second issue."
)


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
    """The minimal correction work spec: issue, hash, failed criteria, findings.

    Deliberately excludes the verifier transcript and the full report. The
    worker re-reads the authoritative work spec from bd; everything else here is
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
        f"CORRECTION ATTEMPT {journal.corrections} for bd issue {issue_id}. "
        "Verification rejected the current candidate. Ortus already claimed this "
        "issue; do not run bd ready, do not select other work, and use only the id "
        f"{issue_id}. Read `bd show {issue_id} --json` for the authoritative work spec, "
        "then correct ONLY the failures below.\n\n"
        f"Issue: {journal.issue_id}\n\n"
        f"Failed acceptance criteria:\n{criteria}\n\n"
    )
    footer = (
        "\n\nCorrect the candidate in place and commit the correction on the issue "
        "branch you are on, with a commit message describing the fix. "
        + _MESSAGE_RULES
        + " Then add a fresh completion comment carrying refreshed `**Changes**` "
        f"and `{_CLAIMS_HEADER}` blocks — one `AC-N: pass` or `AC-N: fail` line "
        "per criterion, stating the result of the check you actually ran; "
        "verification re-runs every check and a claim that disagrees with the "
        "measured result fails the round. Do not close "
        "the issue, do not run git push, git stash, or git reset, do not switch "
        "branches, and do not add a verification comment — verification re-runs "
        "against your corrected candidate and Ortus alone merges and finalizes it. "
        "If a finding needs a product or architecture decision the work spec does not "
        "resolve, do not improvise: report it as a PLAN-GAP and stop. The work spec "
        "itself is frozen for the life of the claim: NEVER edit the claimed issue's "
        "packet (no bd update of its acceptance, design, description, title, or "
        "notes) — verification judges the claim-time criteria by hash, so a packet "
        "edit fails the round no matter how right it is. A finding that indicts the "
        "spec rather than the candidate is report-and-stop: describe the defect in a "
        f"durable comment, run `bd human {issue_id}`, and exit."
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
        "architecture decision the work spec never resolved. An implementation "
        "worker is not allowed to improvise them.\n\n"
        f"Unresolved findings:\n{rendered}\n\n"
        f"Read `bd show {issue_id} --json`, resolve each gap against repository "
        "reality, and update ONLY that issue in place with `bd update` so its "
        "description, design, and acceptance criteria state the resolved decision. "
        "Do not run `bd create`, do not close, replace, or rename any issue, do not "
        "change dependencies, do not edit source files, and do not commit or push. If "
        "the decision genuinely requires a human, leave the work spec unchanged and say "
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
    """Route one planning gap through the planning profile; return its exit code.

    Mirrors the readiness-repair guard: a pass that grows the queue instead of
    updating the named issue is a hard error, not a silent skip.
    """

    gap_log = log.with_name(f"{log.stem}-plan-gap{log.suffix}")
    # Mirror the factory call shape the readiness-repair pass uses so the
    # zero-argument Claude seam existing test overrides rely on keeps working.
    runner = _make_runner() if backend == "claude" else _make_runner(backend)
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
        write_log(f"planning-gap routing: TIMEOUT after {timeout}s; see {gap_log}")
        return 143
    guard_no_replacements(ids_before, {issue["id"] for issue in bd.list_all()})
    if rc != 0:
        write_log(f"planning-gap routing: failed ({backend} exit {rc}); see {gap_log}")
    return rc


_FINALIZATION_MARKER = "## Ortus finalization record"
_FINALIZATION_BLOCKED_PHASE = FINALIZATION_BLOCKED
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
        VERIFIED_PASS,
        _FINALIZATION_BLOCKED_PHASE,
        *(finalized_phase(step) for step in FINALIZATION_STEPS[:-1]),
    }
)


#: Conventional git subject ceiling. A longer issue title is truncated rather
#: than folded into the body, so `git log --oneline`, `git shortlog` and forge
#: UIs stay readable.
_COMMIT_SUBJECT_LIMIT = 72
#: Commit bodies are wrapped at the conventional width, and every quoted block
#: is bounded so a runaway one can't turn the message into an essay.
_COMMIT_BODY_WIDTH = 72
#: One paragraph of stated intent. Generous enough that a real objective lands
#: whole, since a body that explains the change is the point of the message.
_MAX_COMMIT_OBJECTIVE_CHARS = 1200
#: The change description is the part a reader is actually here for, so it gets
#: room to explain rather than a budget that guarantees a sentence dies
#: mid-word. Bounded only against a pathological entry, not against prose.
_MAX_COMMIT_CHANGES_CHARS = 4000
#: Subject text used in place of a title the tracker could not supply, so the
#: subject still reads `<id>: <something>`.
_DEGRADED_COMMIT_SUBJECT = "verified candidate"
#: Markdown headers of the two blocks a completion comment can carry: authored
#: prose about the change, and the structural record of the symbols it touched.
_CHANGES_HEADER = "**Changes**"
_CODEGRAPH_BLOCK_HEADER = "**CodeGraph v1**"
#: A comment carrying either uppercase marker records why work stopped, not
#: what changed. Matching uppercase leaves ordinary prose ("unblocked the
#: queue") selectable.
_NON_DESCRIPTIVE_COMMENT_RE = re.compile(r"\bPLAN-GAP\b|\bBLOCKED\b")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
#: Header of the optional lesson-proposal block a worker may append to its
#: completion comment. Version-pinned like the CodeGraph block: a future
#: schema is skipped rather than parsed against the wrong shape.
_LESSON_PROPOSAL_HEADER = "**Lesson proposal v1**"
#: A proposal key must be a bounded kebab-case slug: it becomes a bd memory
#: key on acceptance and appears verbatim in every contract that injects it.
_PROPOSAL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PROPOSAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _commit_packet(bd: BdClient, issue_id: str) -> dict[str, Any]:
    """The authored work spec for the commit message, or {} when unreadable.

    Every read failure — a tracker that will not answer, a malformed response,
    a missing issue — collapses to the empty dict, so the caller formats a
    degraded message instead of seeing an exception.
    """

    try:
        packet = bd.show(issue_id)
    except Exception:  # noqa: BLE001 - any read failure degrades the subject
        return {}
    return packet if isinstance(packet, dict) else {}


def _undoubled_component(issue_id: str, title: str) -> str:
    """`title` without a leading component the id prefix already names.

    An id like `ortus-4b2p` opens every subject with `ortus`, so a title
    written as `ortus: retire the flag` would say it twice. A title leading
    with any other component is left exactly as written — that word carries
    information the id does not.
    """

    component = issue_id.partition("-")[0].strip().lower()
    head, separator, rest = title.partition(":")
    if not component or not separator or head.strip().lower() != component:
        return title
    return rest.strip() or title


def _commit_subject(issue_id: str, title: str) -> str:
    """`<id>: <title>`, bounded to a conventional subject length.

    The id stays first so existing habits — and any tooling that greps a
    subject for an issue id — keep working. Whitespace is collapsed because a
    title carrying a newline would otherwise split into a spurious body. Only
    the title spends the length budget, so the id prefix stays intact and
    greppable no matter how long the title runs.
    """

    prefix = f"{issue_id}: "
    collapsed = _undoubled_component(issue_id, " ".join(str(title or "").split()))
    budget = max(_COMMIT_SUBJECT_LIMIT - len(prefix), 1)
    return prefix + shortened(collapsed or _DEGRADED_COMMIT_SUBJECT, budget)


def _printable(text: Any) -> str:
    """`text` with anything the terminal encoding can't carry replaced.

    A path recovered with `surrogateescape` raises on encode, which would turn
    an unusual filename into a failed commit.
    """

    return str(text).encode("utf-8", "replace").decode("utf-8", "replace")


def _issue_comments(bd: BdClient, issue_id: str) -> list[str]:
    """Comment bodies oldest first, or [] when they cannot be read.

    Posting order is the contract every caller reads through — the newest
    `**Changes**` block describes what ships, and the newest claims block is
    the one that counts — but bd emits `created_at` at one-second resolution,
    so two comments posted inside the same second come back in either order.
    The comment id breaks that tie: bd issues UUIDv7 ids (probed on this
    tracker — version nibble `7`, as in `019fed0a-4314-781f-...`), whose
    leading 48 bits are a millisecond timestamp, so lexicographic id order is
    posting order within a second. `created_at` stays the primary key, which
    keeps a bd that ever widens its timestamps working unchanged.

    An entry that carries neither field has no key of its own, and sorting it
    on empty strings would hoist it ahead of every stamped comment — the one
    reordering a total order must not perform, because the entry's only
    evidence of when it was posted is the company it arrived in. Each missing
    field therefore inherits the last value seen, and the position bd returned
    the entry in breaks the resulting tie, so an unkeyed entry stays beside
    its neighbours instead of moving to the front.
    """

    try:
        entries = bd.comments(issue_id)
    except Exception:  # noqa: BLE001 - a missing description never blocks a commit
        return []
    keyed: list[tuple[str, str, int, dict[str, Any]]] = []
    stamp = ident = ""
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        stamp = str(entry.get("created_at") or "") or stamp
        ident = str(entry.get("id") or "") or ident
        keyed.append((stamp, ident, position, entry))
    keyed.sort(key=lambda row: (row[0], row[1], row[2]))
    bodies: list[str] = []
    for *_key, entry in keyed:
        for key in ("body", "text", "comment", "content"):
            value = entry.get(key)
            if value:
                bodies.append(str(value))
                break
    return bodies


def _describes_the_change(text: str) -> bool:
    """True when a comment is an implementer's record of what it changed.

    A routed planning gap, a stopped-work note and Ortus's own finalization record
    are excluded even when they quote a `**Changes**` header: committing one of
    those as the description would say something the change does not contain.
    """

    if _FINALIZATION_MARKER in text or _NON_DESCRIPTIVE_COMMENT_RE.search(text):
        return False
    return _CHANGES_HEADER in text


def _block_lines(comment: str, header: str) -> list[str]:
    """Lines under `header`, up to the next bolded header or the end."""

    lines: list[str] = []
    inside = False
    for line in comment.splitlines():
        stripped = line.strip()
        if not inside:
            inside = stripped.startswith(header)
            continue
        if stripped.startswith("**"):
            break
        lines.append(line)
    return lines


def _changes_bullets(comment: str) -> list[str]:
    """Bullets of the comment's `**Changes**` block, wrapped lines rejoined."""

    bullets: list[str] = []
    for line in _block_lines(comment, _CHANGES_HEADER):
        match = _BULLET_RE.match(line)
        if match:
            bullets.append(" ".join(match.group(1).split()))
        elif line.strip() and bullets:
            bullets[-1] += " " + " ".join(line.split())
    return [bullet for bullet in bullets if bullet]


def _codegraph_summary(comment: str) -> str:
    """The `modified:`/`new:` entries of a `**CodeGraph v1**` block as body text.

    Only the exact schema header is read, so a comment carrying a later schema
    contributes nothing rather than being parsed against the wrong shape.
    """

    fields: dict[str, str] = {}
    for line in _block_lines(comment, _CODEGRAPH_BLOCK_HEADER):
        key, separator, value = line.strip().partition(":")
        key, value = key.strip().lower(), value.strip()
        if not separator or key not in ("modified", "new") or not value:
            continue
        if value.lower() == "none" or value.lower().startswith("none "):
            continue
        fields[key] = value
    labelled = [
        ("Modified: ", fields.get("modified", "")),
        ("Added: ", fields.get("new", "")),
    ]
    return _bounded_block(
        [f"{label}{value}" for label, value in labelled if value], prefix=""
    )


def _bounded_block(entries: list[str], *, prefix: str = "- ") -> str:
    """`entries` wrapped for a commit body, cut off once the budget is spent."""

    rendered: list[str] = []
    budget = _MAX_COMMIT_CHANGES_CHARS
    for entry in entries:
        text = shortened(_printable(entry), _MAX_COMMIT_CHANGES_CHARS)
        rendered.append(
            textwrap.fill(
                text,
                width=_COMMIT_BODY_WIDTH,
                initial_indent=prefix,
                subsequent_indent=" " * len(prefix),
            )
        )
        budget -= len(text)
        if budget <= 0:
            break
    return "\n".join(rendered)


def _completion_comments(bd: BdClient, issue_id: str) -> list[str]:
    """Implementer records of what changed, oldest first.

    Read without any degradation logging of its own: both the deterministic
    body and the composition pass want this text, and only the former treats
    its absence as a degradation worth a line in the run log.
    """

    return [
        text for text in _issue_comments(bd, issue_id) if _describes_the_change(text)
    ]


def _lesson_proposals(comment: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Every `**Lesson proposal v1**` block in `comment`, plus malformation notes.

    Returns ``(proposals, malformed)`` where each proposal is ``(key, body)``
    with the proposal's date folded into the body — the memory store is flat,
    so the date the contract requires must travel inside the text. A block
    missing a field, carrying an invalid key or date, or cut short by a stray
    `**` delimiter inside its own text yields a note instead of a proposal;
    nothing in this comment can make parsing raise.
    """

    proposals: list[tuple[str, str]] = []
    malformed: list[str] = []
    fields: dict[str, str] | None = None

    def _flush() -> None:
        nonlocal fields
        if fields is None:
            return
        key = fields.get("key", "")
        lesson = " ".join(fields.get("lesson", "").split())
        date = fields.get("date", "")
        if not _PROPOSAL_KEY_RE.match(key):
            malformed.append(f"key {key!r} is not a bounded kebab-case slug")
        elif not lesson:
            malformed.append(f"proposal {key!r} carries no lesson text")
        elif not _PROPOSAL_DATE_RE.match(date):
            malformed.append(f"proposal {key!r} has no YYYY-MM-DD date")
        else:
            proposals.append((key, f"{lesson} ({date})"))
        fields = None

    for line in comment.splitlines():
        stripped = line.strip()
        if stripped.startswith(_LESSON_PROPOSAL_HEADER):
            _flush()
            fields = {}
            continue
        if fields is None:
            continue
        if stripped.startswith("**"):
            _flush()
            continue
        name, separator, value = stripped.partition(":")
        if separator and name.strip().lower() in ("key", "lesson", "date"):
            fields[name.strip().lower()] = value.strip()
    _flush()
    return proposals, malformed


def _record_lesson_proposals(
    bd: BdClient, issue_id: str, *, write_log: Callable[[str], None]
) -> None:
    """Record the issue's well-formed lesson proposals as pending curation.

    A comment with no proposal block is skipped without a log line — proposing
    nothing is the normal case and must cost nothing. A malformed block is
    worth a log line and nothing more, and a tracker failure is logged and
    abandoned: no proposal is ever worth failing the run that carried it.
    """

    try:
        for comment in _issue_comments(bd, issue_id):
            if _LESSON_PROPOSAL_HEADER not in comment:
                continue
            proposals, malformed = _lesson_proposals(comment)
            for note in malformed:
                write_log(
                    f"lesson proposal: ignored a malformed block on {issue_id} "
                    f"— {note}"
                )
            for key, body in proposals:
                if bd.propose_lesson(key, body):
                    write_log(
                        f"lesson proposal: recorded {key!r} from {issue_id}; "
                        "pending until curated"
                    )
                else:
                    write_log(
                        f"lesson proposal: {key!r} from {issue_id} is already "
                        "covered by an accepted lesson"
                    )
    except Exception as exc:  # noqa: BLE001 - a lost proposal never fails the run
        first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        write_log(f"lesson proposal: recording failed for {issue_id} ({first})")


def _change_description(
    bd: BdClient,
    issue_id: str,
    journal: CandidateJournal,
    *,
    write_log: Callable[[str], None],
) -> str:
    """What the commit changed, from the first source that states it.

    Two sources, in descending order of how much they say: the implementer's
    `**Changes**` bullets, and the `**CodeGraph v1**` block of that same
    comment. The bullets are authored before the code is re-checked, so a run
    that went back and edited the code needs one `**Changes**` comment per
    round for them to describe what is being committed; short of that the
    structural block speaks instead, and short of that the message carries only
    what the issue itself already said.

    No source enumerates the committed files: `git show --stat` prints them
    from the commit itself, so a list in the body could only agree with it or
    be wrong. Falling back at all means the prose the commit was supposed to
    carry was never written, which is a degradation worth a line in the run
    log rather than a silently thinner message.
    """

    described = _completion_comments(bd, issue_id)
    latest = described[-1] if described else ""
    if latest and len(described) > journal.corrections:
        bullets = _changes_bullets(latest)
        if bullets:
            return _bounded_block(bullets)
    summary = _codegraph_summary(latest) if latest else ""
    write_log(
        f"finalization: no usable **Changes** bullets for {issue_id}; the commit "
        "body degrades to "
        + ("the CodeGraph block" if summary else "the issue objective alone")
    )
    return summary


def _commit_message(issue_id: str, packet: dict[str, Any], description: str) -> str:
    """`<id>: <title>`, the issue's objective, and what the commit changed.

    Both body blocks are text someone else already wrote down — the authored
    work spec and `description` — so the message states nothing that was not
    recorded before the commit ran. An unreadable work spec still commits: the
    subject degrades and the description stands alone.
    """

    subject = _commit_subject(issue_id, str(packet.get("title") or ""))
    blocks: list[str] = []
    objective = " ".join(section_text(packet.get("description"), "Objective").split())
    if objective:
        blocks.append(
            textwrap.fill(
                shortened(objective, _MAX_COMMIT_OBJECTIVE_CHARS),
                width=_COMMIT_BODY_WIDTH,
            )
        )
    body = _printable(description).strip()
    if body:
        blocks.append(body)
    if not blocks:
        return f"{subject}\n"
    return f"{subject}\n\n" + "\n\n".join(blocks) + "\n"


#: Journaled in place of a message when the composition pass produced none, so
#: the phase transition still reads as landed and a restart does not spend a second
#: model call re-asking a question that already failed.
_COMPOSE_UNAVAILABLE = "unavailable"

#: What the composition pass is handed and what it returns: the journal it is
#: describing plus the authored work-spec fields, and the full commit message text
#: or a `ComposeFailed`.
ComposeCallable = Callable[..., str]


def _composed_message(journal: CandidateJournal) -> str:
    """The message a prior compose phase transition journaled, if it produced one."""

    value = journal.finalization.get("compose")
    if not isinstance(value, str) or value == _COMPOSE_UNAVAILABLE:
        return ""
    return value


def _authority_state(
    bd: BdClient,
    git: GitClient,
    journal: CandidateJournal,
    *,
    repo: Path,
    issue_id: str,
) -> dict[str, str]:
    """Everything a read-only pass must leave exactly as it found it.

    The candidate's own bytes, the shape of the worktree around it (so a file
    created beside the candidate is caught too), and the issue's status.

    The tracker is read first and its generated exports are excluded from the
    worktree shape: bd rewrites those as a side effect of being asked anything,
    including by this function, and comparing them would report the reader's
    own footprints as the pass's.
    """

    state: dict[str, str] = {}
    try:
        state["tracker"] = str(bd.status(issue_id) or "")
    except Exception:  # noqa: BLE001 - an unreadable tracker is compared as such
        state["tracker"] = "unreadable"
    for path, fingerprint in fingerprint_paths(
        repo, journal.candidate_paths
    ).items():
        state[f"path:{path}"] = fingerprint
    dirty = (git.dirty_paths() if git.is_git_repo() else frozenset()) or frozenset()
    state["worktree"] = ", ".join(sorted(dirty - _TRACKER_EXPORT_PATHS))
    return state


def _finalization_report(issue_id: str, journal: CandidateJournal) -> str:
    return (
        f"{_FINALIZATION_MARKER}\n\n"
        f"Issue: {issue_id}\n"
        f"Base commit: `{journal.base_head}`\n"
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
    candidate_git: GitClient | None = None,
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
    # authoritative work-spec hash, and grind's own report comment legitimately
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
    # The transaction's own issue branch is the one legitimate non-integration
    # location: the claim checked it out, and the commit step lands there
    # before fast-forwarding the integration branch.
    if git.has_commits() and branch != integration_branch:
        if not (journal.issue_branch and branch == journal.issue_branch):
            return (
                f"working tree is on {branch or 'a detached HEAD'}, "
                f"not {integration_branch}"
            )
    if journal.finalized("commit"):
        return None
    # The candidate's own state — its branch, its worktree, its diff — lives
    # in the worker workspace when one exists; the integration ref and the
    # primary checkout are always the primary repository's.
    side = candidate_git or git
    base = journal.base_head if journal.issue_branch else ""
    if journal.issue_branch:
        tip = side.branch_tip(journal.issue_branch)
        integration_tip = git.branch_tip(integration_branch)
        if (
            tip
            and tip != journal.base_head
            and git.head_oid() == tip
            and journal.finalized("close")
        ):
            # Finalization was already mid-flight (close journaled) and its
            # own commit — possibly the fast-forward too — landed before a
            # crash reached the phase-transition write. The worktree re-checks below
            # would misread the committed candidate; the commit step
            # re-journals from this same observable state. This deliberately
            # skips a second integrity pass on replay: the candidate was
            # re-validated on the entry that performed the close.
            return None
        if journal.branch_head and tip != journal.branch_head:
            return (
                f"issue branch {journal.issue_branch} moved after the passing "
                f"verdict ({journal.branch_head[:12]} → {(tip or 'gone')[:12]})"
            )
        if integration_tip not in (journal.base_head, tip):
            return (
                f"{integration_branch} moved after the passing verdict; the "
                f"fast-forward from {journal.base_head[:12]} no longer applies"
            )
    elif side.head_oid() != journal.base_head:
        return (
            f"base commit {journal.base_head} is no longer HEAD; the candidate was "
            "verified against a different tree"
        )
    dirty = side.dirty_paths()
    if dirty is None:
        return "could not read the worktree before finalization"
    range_changed: frozenset[str] = frozenset()
    if base and side.head_oid() != base:
        changed = side.changed_paths(base)
        if changed is None:
            return "could not read the candidate's committed range"
        range_changed = changed
    owned = _candidate_paths(dirty | range_changed, baseline)
    if owned != frozenset(journal.candidate_paths):
        drifted = sorted(owned.symmetric_difference(journal.candidate_paths))
        return (
            "candidate path set changed after the passing verdict: "
            + ", ".join(drifted)
        )
    try:
        if (
            sha256_bytes(candidate_diff(side.repo, owned, base=base))
            != journal.candidate_hash
        ):
            return "candidate changed after the passing verdict"
    except RuntimeError as exc:
        return f"could not re-hash the candidate ({exc})"
    return None


def _message_composer(
    *,
    repo: Path,
    log: Path,
    backend: str,
    profile: AgentProfile,
    capability: CodeGraphCapability | None,
    timeout: float | None,
    write_log: Callable[[str], None],
) -> ComposeCallable:
    """Bind the composition pass to this run's backend, log, and profile.

    Finalization is handed a callable rather than a runner so it stays a
    tracker-and-git routine: it decides when a message is wanted and what
    happens when there isn't one, and knows nothing about how one is obtained.

    `write_log` is here only so a repaired message says so in the run log. A
    shortened subject is still a message that landed, so it is not a failure
    finalization has to react to — but a composer that keeps writing past the
    limit is worth seeing, and a silent repair would hide it.
    """

    def _compose(
        journal: CandidateJournal,
        *,
        issue_id: str,
        packet: dict[str, Any],
        changes: list[str],
    ) -> str:
        reference = journal.candidate_diff_ref
        if not reference:
            raise ComposeFailed("the transaction recorded no candidate diff")
        artifact = Path(reference)
        if not artifact.is_absolute():
            artifact = repo / artifact
        try:
            diff = artifact.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ComposeFailed(
                f"the candidate diff at {reference} is unreadable ({exc})"
            ) from exc
        return compose_commit_message(
            repo,
            issue_id=issue_id,
            title=str(packet.get("title") or ""),
            objective=section_text(packet.get("description"), "Objective"),
            changes=changes[-1] if changes else "",
            diff=diff,
            log_path=log,
            backend=backend,
            profile=profile,
            capability=capability,
            timeout=timeout,
            # The same indirection every other spawn in this module goes
            # through, so one patch point still swaps the whole backend.
            runner_factory=_make_runner,
            note=lambda text: write_log(
                f"finalization: commit-message pass for {issue_id}: {text}"
            ),
        )

    return _compose


def _refresh_tracker_exports(
    bd: BdClient, write_log: Callable[[str], None]
) -> str | None:
    """Regenerate the tracker exports right before they are staged.

    Under a bd that supports on-demand export, Ortus owns export timing:
    the file is rewritten at the exact moment its bytes are consumed, so
    ambient-timing races cannot recur and bd releases that no longer write
    the export as a side effect stay compatible. A bd without the command
    keeps its ambient regime byte-identical. Failure is a blocker: an issue
    must never commit knowingly stale tracker state.
    """

    try:
        if not bd.supports_export():
            return None
        reason = bd.export_issues()
    except Exception as exc:  # noqa: BLE001 - a broken bd is a blocker, not a crash
        reason = str(exc)
    if reason:
        return f"could not regenerate the tracker exports: {reason}"
    write_log("finalization: tracker exports regenerated via bd export")
    return None


def _land_from_workspace(
    bd: BdClient,
    git: GitClient,
    workspace_git: GitClient,
    store: JournalStore,
    journal: CandidateJournal,
    *,
    issue_id: str,
    integration_branch: str,
    write_log: Callable[[str], None],
    merge_gate: bool = False,
    merge_gate_timeout: float = DEFAULT_MERGE_GATE_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CandidateJournal, str | None]:
    """Land a workspace-isolated candidate without moving the primary checkout.

    Fold the transaction's late files into the branch commit in the worker's
    clone, hold the message to its contract there, fetch the branch into the
    primary repository, and fast-forward the integration branch under its own
    never-moved checkout, exports carried.
    """

    branch = journal.issue_branch
    if workspace_git.current_branch() != branch:
        return journal, (
            "the worker workspace is on "
            f"{workspace_git.current_branch() or 'a detached HEAD'}, "
            f"not {branch}"
        )
    if workspace_git.head_oid() == journal.base_head:
        return journal, (
            f"{branch} carries no commits beyond the base; nothing to land"
        )
    primary_tip_before = git.branch_tip(branch)
    export_blocker = _refresh_tracker_exports(bd, write_log)
    if export_blocker is not None:
        return journal, export_blocker
    primary_dirty = git.dirty_paths()
    if primary_dirty is None:
        return journal, "could not read the primary worktree before committing"
    late_exports = sorted(primary_dirty & _TRACKER_EXPORT_PATHS)
    for rel in late_exports:
        source = git.repo / rel
        dest = workspace_git.repo / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
        except OSError as exc:
            return journal, f"could not carry {rel} into the workspace ({exc})"
    if late_exports:
        if not workspace_git.amend_paths(frozenset(late_exports)):
            return journal, (
                "could not fold the transaction's late paths into "
                "the worker's commit: " + ", ".join(late_exports)
            )
        write_log(
            "finalization: folded into the worker's commit: "
            + ", ".join(late_exports)
        )
    raw = workspace_git.head_message().strip()
    packet = _commit_packet(bd, issue_id)
    subject_line, _, rest = raw.partition("\n")
    bare = subject_line.removeprefix(f"{issue_id}: ")
    try:
        diff_text = candidate_diff(
            workspace_git.repo,
            frozenset(journal.candidate_paths),
            base=journal.base_head,
        ).decode("utf-8", errors="replace")
    except RuntimeError:
        diff_text = ""
    try:
        validated = validate_message(
            CommitMessage(subject=bare, body=rest.strip()),
            issue_id=issue_id,
            title=str(packet.get("title") or ""),
            diff=diff_text,
        )
        if validated.text != raw:
            if not workspace_git.amend_message(validated.text):
                return journal, "could not amend the worker's commit message"
            note = (
                f"shortened from {validated.shortened_from} characters"
                if validated.shortened_from
                else "normalized"
            )
            write_log(
                f"finalization: worker commit message {note} for {issue_id}"
            )
    except ComposeRejected as exc:
        write_log(
            f"finalization: worker commit message rejected ({exc}); "
            "replaced by the deterministic assembly"
        )
        replacement = _commit_message(
            issue_id,
            packet,
            _change_description(bd, issue_id, journal, write_log=write_log),
        )
        if not workspace_git.amend_message(replacement):
            return journal, "could not amend the worker's commit message"
    branch_head = workspace_git.head_oid()
    journal = journal.with_branch(branch, branch_head)
    store.save(journal)
    fetch_reason = git.fetch_branch(workspace_git.repo, branch)
    if fetch_reason and primary_tip_before:
        # The fold and message repair amended the harness's own commits, so
        # the primary's earlier backup of this branch trails a rewrite; it is
        # replaced only if it still sits where this transaction last put it.
        fetch_reason = git.replace_branch(
            workspace_git.repo, branch, expected_tip=primary_tip_before
        )
    if fetch_reason:
        return journal, (
            f"could not fetch {branch} from the worker workspace: {fetch_reason}"
        )
    journal, gate_blocker = _apply_merge_gate(
        git,
        store,
        journal,
        issue_branch=branch,
        merge_gate=merge_gate,
        merge_gate_timeout=merge_gate_timeout,
        write_log=write_log,
        sleep=sleep,
        clock=clock,
    )
    if gate_blocker is not None:
        return journal, gate_blocker
    advance_reason = git.advance_preserving_exports(
        branch, _TRACKER_EXPORT_PATHS
    )
    if advance_reason:
        return journal, (
            f"fast-forward of {integration_branch} to {branch} is not "
            f"possible ({advance_reason}); the commit is preserved on "
            f"{branch} — resolve and re-run grind"
        )
    write_log(
        f"finalization: {integration_branch} fast-forwarded to "
        f"{branch} at {branch_head[:12]}"
    )
    journal = journal.with_finalization("commit", git.head_oid())
    store.save(journal)
    write_log(
        f"finalization: committed owned paths for {issue_id}: "
        + (", ".join(late_exports) or "none")
    )
    return journal, None


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
    compose: ComposeCallable | None = None,
    workspace_git: GitClient | None = None,
    merge_gate: bool = False,
    merge_gate_timeout: float = DEFAULT_MERGE_GATE_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CandidateJournal, str | None]:
    """Ortus-owned report → close → compose → commit → sync, journaled by step.

    Returns the updated journal and a blocker string when finalization could
    not complete. Every step is skipped when its phase transition is already
    journaled OR when observable state already shows it landed, so a restart at
    any phase transition produces no duplicate comment, close, commit, or push.

    `compose` is the one place a model participates in finalization: it writes
    the commit message from the sealed diff and returns text. It is optional,
    it decides nothing, and every way it can fail ends in the deterministic
    body — the commit lands either way.
    """

    blocker = _finalization_blocker(
        bd,
        git,
        journal,
        repo=repo,
        issue_id=issue_id,
        integration_branch=integration_branch,
        baseline=baseline,
        candidate_git=workspace_git,
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

    if not journal.finalized("compose"):
        composed = ""
        if compose is None:
            write_log(
                f"finalization: commit-message pass retired for {issue_id}; "
                "the worker's own message or the deterministic assembly stands"
            )
        else:
            before = _authority_state(
                bd, git, journal, repo=repo, issue_id=issue_id
            )
            failure = ""
            try:
                composed = compose(
                    journal,
                    issue_id=issue_id,
                    packet=_commit_packet(bd, issue_id),
                    changes=_completion_comments(bd, issue_id),
                )
            except ComposeFailed as exc:
                failure = str(exc)
            except Exception as exc:  # noqa: BLE001 - prose is never worth a commit
                failure = f"unexpected error ({exc})"
            # Checked whether the pass succeeded or not: a pass that failed
            # after writing is exactly the case where trusting its own report
            # would be wrong.
            after = _authority_state(bd, git, journal, repo=repo, issue_id=issue_id)
            try:
                guard_read_only(before, after)
            except ComposeExceededAuthority as exc:
                write_log(f"finalization: HALT — {exc}")
                journal = replace(journal, phase=_FINALIZATION_BLOCKED_PHASE)
                store.save(journal)
                return journal, str(exc)
            if failure:
                write_log(
                    f"finalization: commit-message pass for {issue_id} produced "
                    f"nothing usable ({failure}); the deterministic body stands"
                )
        journal = journal.with_finalization(
            "compose", composed or _COMPOSE_UNAVAILABLE
        )
        store.save(journal)
        if composed:
            write_log(f"finalization: commit message composed for {issue_id}")

    if not journal.finalized("commit"):
        if not git.is_git_repo():
            journal = journal.with_finalization("commit", "not-a-git-repo")
            store.save(journal)
        elif (
            workspace_git is not None
            and journal.issue_branch
            and workspace_git.head_oid() != journal.base_head
        ):
            # A workspace whose branch never advanced (a verified no-op —
            # e.g. a repaired work spec whose behavior was already correct)
            # has nothing to fetch; the legacy primary-side path below
            # commits the transaction's late files and closes as before.
            # Workspace-isolated landing: the branch and its worktree live in
            # the worker's clone. The transaction's late files — the tracker
            # exports the close step rewrote primary-side — fold into the
            # branch commit there, the message meets its contract there, and
            # only then does the branch come home: a fetch into the primary
            # repository and a fast-forward of the integration branch under
            # the checkout that never moved.
            journal, workspace_blocker = _land_from_workspace(
                bd,
                git,
                workspace_git,
                store,
                journal,
                issue_id=issue_id,
                integration_branch=integration_branch,
                write_log=write_log,
                merge_gate=merge_gate,
                merge_gate_timeout=merge_gate_timeout,
                sleep=sleep,
                clock=clock,
            )
            if workspace_blocker is not None:
                return journal, workspace_blocker
        else:
            # The commit belongs on the issue branch. A resumed finalization
            # may arrive here on the integration branch (the startup guard
            # re-asserts it); the branch exists from the claim and sits at the
            # same commit, so a checkout carries the dirty candidate along.
            if (
                journal.issue_branch
                and git.branch_tip(journal.issue_branch)
                and git.current_branch() != journal.issue_branch
                and git.head_oid() == journal.base_head
            ):
                if not git.checkout(journal.issue_branch):
                    return journal, (
                        f"could not check out {journal.issue_branch} to commit "
                        "the candidate"
                    )
            export_blocker = _refresh_tracker_exports(bd, write_log)
            if export_blocker is not None:
                return journal, export_blocker
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
            worker_committed = bool(
                journal.issue_branch
                and git.current_branch() == journal.issue_branch
                and git.head_oid() != journal.base_head
            )
            if not worker_committed:
                message = _composed_message(journal)
                if message:
                    write_log(
                        f"finalization: committing {issue_id} with the composed "
                        "message"
                    )
                else:
                    packet = _commit_packet(bd, issue_id)
                    if not packet:
                        write_log(
                            "finalization: work spec unreadable; committing "
                            f"{issue_id} with a degraded subject"
                        )
                    message = _commit_message(
                        issue_id,
                        packet,
                        _change_description(
                            bd, issue_id, journal, write_log=write_log
                        ),
                    )
                committed = git.commit_paths(stage, message)
                if not committed:
                    # git already said why microseconds ago; carrying its text
                    # out of here is the difference between an operator acting
                    # on the cause and reproducing the failure by hand
                    # (ortus-pgqg).
                    blocked = "path-scoped commit of the owned candidate failed"
                    if committed.reason:
                        blocked = f"{blocked}; {committed.reason}"
                    write_log(f"finalization: HALT — {blocked}")
                    return journal, blocked
            else:
                # The head commit is the worker's own. The transaction's late
                # files — the tracker exports the close step just rewrote, and
                # any owned edits the worker left uncommitted — fold into that
                # commit rather than stacking a second one, so an issue still
                # lands as exactly one commit. The message then passes the
                # same deterministic gate a composed message did: an over-long
                # subject is shortened, and a message that is wrong rather
                # than long is replaced by the deterministic assembly — by
                # amend, so the tree is never touched beyond the fold.
                if stage:
                    if not git.amend_paths(stage):
                        return journal, (
                            "could not fold the transaction's late paths into "
                            "the worker's commit: " + ", ".join(sorted(stage))
                        )
                    write_log(
                        "finalization: folded into the worker's commit: "
                        + ", ".join(sorted(stage))
                    )
                raw = git.head_message().strip()
                packet = _commit_packet(bd, issue_id)
                subject_line, _, rest = raw.partition("\n")
                bare = subject_line.removeprefix(f"{issue_id}: ")
                try:
                    diff_text = candidate_diff(
                        repo,
                        frozenset(journal.candidate_paths),
                        base=journal.base_head,
                    ).decode("utf-8", errors="replace")
                except RuntimeError:
                    diff_text = ""
                try:
                    validated = validate_message(
                        CommitMessage(subject=bare, body=rest.strip()),
                        issue_id=issue_id,
                        title=str(packet.get("title") or ""),
                        diff=diff_text,
                    )
                    if validated.text != raw:
                        if not git.amend_message(validated.text):
                            return journal, (
                                "could not amend the worker's commit message"
                            )
                        note = (
                            "shortened from "
                            f"{validated.shortened_from} characters"
                            if validated.shortened_from
                            else "normalized"
                        )
                        write_log(
                            f"finalization: worker commit message {note} "
                            f"for {issue_id}"
                        )
                except ComposeRejected as exc:
                    write_log(
                        f"finalization: worker commit message rejected ({exc}); "
                        "replaced by the deterministic assembly"
                    )
                    replacement = _commit_message(
                        issue_id,
                        packet,
                        _change_description(
                            bd, issue_id, journal, write_log=write_log
                        ),
                    )
                    if not git.amend_message(replacement):
                        return journal, (
                            "could not amend the worker's commit message"
                        )
                # An amend moves the tip; keep the journal's record current so
                # a crash before the fast-forward replays instead of reading
                # the amended tip as post-verdict movement.
                journal = journal.with_branch(journal.issue_branch, git.head_oid())
                store.save(journal)
            if journal.issue_branch and git.current_branch() == journal.issue_branch:
                # The commit exists on the issue branch — its durable home.
                # The integration ref is fast-forwarded first, without
                # touching the working tree, so the checkout that follows
                # switches between two names for the same commit and cannot
                # conflict with concurrently-dirtied files. Anything short of
                # a fast-forward is a blocker to report, and the branch keeps
                # the commit either way; it is deliberately never deleted.
                branch_head = git.head_oid()
                journal = journal.with_branch(journal.issue_branch, branch_head)
                store.save(journal)
                journal, gate_blocker = _apply_merge_gate(
                    git,
                    store,
                    journal,
                    issue_branch=journal.issue_branch,
                    merge_gate=merge_gate,
                    merge_gate_timeout=merge_gate_timeout,
                    write_log=write_log,
                    sleep=sleep,
                    clock=clock,
                )
                if gate_blocker is not None:
                    return journal, gate_blocker
                if not git.fast_forward(integration_branch, journal.issue_branch):
                    return journal, (
                        f"fast-forward of {integration_branch} to "
                        f"{journal.issue_branch} is not possible (the "
                        "integration branch moved); the commit is preserved "
                        f"on {journal.issue_branch} — resolve and re-run grind"
                    )
                if not git.checkout(integration_branch):
                    return journal, (
                        f"{integration_branch} was fast-forwarded to "
                        f"{journal.issue_branch} but could not be checked "
                        "out; check the working tree and re-run grind"
                    )
                write_log(
                    f"finalization: {integration_branch} fast-forwarded to "
                    f"{journal.issue_branch} at {branch_head[:12]}"
                )
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
            pushed = _announced_push(git, integration_branch)
            if not pushed:
                write_log(
                    "finalization: push rejected; pulling --rebase and retrying once"
                )
                output.progress(
                    "grind", "push rejected; rebasing on origin and retrying"
                )
                if git.pull_rebase(integration_branch):
                    pushed = _announced_push(git, integration_branch)
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



def _prepare_issue_branch(
    git: GitClient,
    *,
    issue_id: str,
    integration_branch: str,
    journal: CandidateJournal | None,
    write_log: Callable[[str], None],
) -> tuple[str, str, bool]:
    """Put the tree on `ortus/<issue-id>`, or say why that is impossible.

    Returns ``(branch_name, blocker, resumed)`` — blocker is "" on success,
    and ``resumed`` is True only when an existing branch was checked out with
    its commits intact. Callers use it to keep one invariant true: the
    journal's ``base_head`` equals the branch's fork point — preserved when
    the branch survives, refreshed when it is (re)established at the
    integration head (ortus-ti4i: a preserved base for a re-cut branch made
    the integration-moved guard reject every retry). Owns the
    whole branch discipline of a claim:

    - A tree stranded on a prior issue's branch is returned to the
      integration branch first, carrying the tracker exports across (they
      are generated, candidate-excluded, and continuously rewritten by
      intake, so their committed copies legitimately diverge from the
      worktree's newest bytes — a plain checkout refused exactly that on
      2026-08-11 and halted the run).
    - Resuming the journal's own issue checks its existing branch out when
      the tip still matches the journal's recorded head — the branch is the
      durable home the keystone promised, commits and all.
    - Any other pre-existing branch is reused only at the integration head,
      reported otherwise, never reset.
    - A branch created by this claim is removed again when the claim fails,
      so a failed claim cannot strand a ref the never-reset rule would later
      refuse.
    - Failures carry git's own words (ortus-pgqg).
    """

    issue_branch = f"ortus/{issue_id}"
    created_this_claim = False
    blocker = ""

    resuming_own = bool(
        journal is not None
        and journal.issue_id == issue_id
        and journal.issue_branch == issue_branch
        and git.branch_exists(issue_branch)
        and journal.branch_head
        and git.branch_tip(issue_branch) == journal.branch_head
    )

    if git.current_branch() != integration_branch and not (
        resuming_own and git.current_branch() == issue_branch
    ):
        switch_reason = git.switch_preserving_exports(
            integration_branch, _TRACKER_EXPORT_PATHS
        )
        if switch_reason:
            return issue_branch, (
                f"could not return to {integration_branch} "
                f"before claiming: {switch_reason}"
            ), False
        write_log(
            f"iter prep: reasserted {integration_branch} "
            "(exports carried) before claiming"
        )

    if not git.valid_branch_name(issue_branch):
        return issue_branch, (
            f"issue id {issue_id!r} is not a legal branch "
            f"name component for {issue_branch!r}"
        ), False

    if resuming_own:
        if git.current_branch() != issue_branch:
            reason = git.switch_preserving_exports(
                issue_branch, _TRACKER_EXPORT_PATHS
            )
            if reason:
                return issue_branch, (
                    f"could not check out {issue_branch} to resume: {reason}"
                ), False
        integration_tip = git.branch_tip(integration_branch)
        if (
            journal is not None
            and journal.base_head
            and integration_tip
            and journal.base_head != integration_tip
        ):
            # The workspace path already rebases a parked branch forward
            # (ortus-bz3c). The legacy shared-tree path did not, so a stale
            # fork point burned a full worker and then failed the
            # integration-moved guard on every retry (ortus-o52d).
            dirty = (
                (git.dirty_paths() or frozenset())
                - _TRACKER_EXPORT_PATHS
                - _TRACKER_TOOL_STATE
            )
            if dirty:
                return issue_branch, (
                    f"{integration_branch} moved past {issue_branch}'s fork "
                    f"point ({journal.base_head[:12]} → "
                    f"{integration_tip[:12]}) and the worktree is dirty, so "
                    "the branch cannot rebase forward; a worker would fail "
                    "the integration-moved guard. Dirty paths: "
                    + ", ".join(sorted(dirty))
                    + ". Resolve the branch manually, then re-run grind"
                ), False
            rebase_reason = git.rebase_onto(integration_tip, issue_branch)
            if rebase_reason:
                return issue_branch, (
                    f"{integration_branch} moved past {issue_branch}'s fork "
                    f"point and the rebase forward hit a conflict: "
                    f"{rebase_reason} — resolve the branch manually, then "
                    "re-run grind"
                ), False
            write_log(
                f"iter prep: rebased parked {issue_branch} onto "
                f"{integration_branch} at {integration_tip[:12]}"
            )
            # A rebase is a re-cut: the caller refreshes base_head from the
            # integration tip, not this checkout (we are on the issue branch).
            return issue_branch, "", False
        write_log(
            f"iter prep: resumed existing {issue_branch} at "
            f"{git.branch_tip(issue_branch)[:12]}"
        )
        return issue_branch, "", True

    if git.branch_exists(issue_branch):
        if git.branch_tip(issue_branch) != git.head_oid():
            return issue_branch, (
                f"branch {issue_branch} already exists at "
                f"{git.branch_tip(issue_branch)[:12]}, not at the "
                f"integration head {git.head_oid()[:12]}; refusing "
                "to reuse or reset it — resolve the branch "
                "manually, then re-run grind"
            ), False
        write_log(
            f"iter prep: reusing existing {issue_branch} at the integration head"
        )
    elif git.create_branch(issue_branch, integration_branch):
        created_this_claim = True
    else:
        blocker = f"could not create {issue_branch}"

    if not blocker:
        reason = git.checkout_reporting(issue_branch)
        if reason:
            blocker = f"could not check out {issue_branch}: {reason}"

    if blocker and created_this_claim and git.delete_merged_branch(issue_branch):
        write_log(
            f"iter prep: removed just-created {issue_branch} after the failed claim"
        )
    return issue_branch, blocker, False


#: Where per-issue worker workspaces live: inside the primary repository so
#: relative journal paths stay portable, under logs/ so git ignores them.
_WORKSPACES_DIR = Path("logs") / "grind-workspaces"


def _materialize_workspace(
    git: GitClient,
    *,
    repo: Path,
    issue_id: str,
    integration_branch: str,
    journal: CandidateJournal | None,
    write_log: Callable[[str], None],
) -> tuple[str, Path | None, GitClient | None, str, bool]:
    """Give the worker a disposable shared clone on `ortus/<issue-id>`.

    Returns ``(branch, workspace, workspace_git, blocker, resumed)`` —
    blocker is "" on success, ``resumed`` is True only when a pre-existing
    branch was checked out with its commits intact, and the primary
    checkout never moves: the same claim discipline `_prepare_issue_branch`
    owned, executed inside a clone so operator intake and the candidate
    can no longer collide.

    - The primary tree is returned to the integration branch first if a
      pre-clone-era crash stranded it elsewhere (exports carried).
    - The primary repository's branch ref decides resume vs fresh, under
      the same never-reset rules: a resumed journal's branch is adopted
      commits and all; any other pre-existing branch is reused only at the
      integration head.
    - The clone is cut at the integration head, the branch materializes
      inside it (from the primary's ref when one exists), and the
      CodeGraph index — gitignored, so absent from any clone — is
      symlinked from the primary so workers keep their index.
    """

    issue_branch = f"ortus/{issue_id}"
    if git.current_branch() != integration_branch:
        switch_reason = git.switch_preserving_exports(
            integration_branch, _TRACKER_EXPORT_PATHS
        )
        if switch_reason:
            return issue_branch, None, None, (
                f"could not return to {integration_branch} "
                f"before claiming: {switch_reason}"
            ), False
        write_log(
            f"iter prep: reasserted {integration_branch} "
            "(exports carried) before claiming"
        )
    if not git.valid_branch_name(issue_branch):
        return issue_branch, None, None, (
            f"issue id {issue_id!r} is not a legal branch "
            f"name component for {issue_branch!r}"
        ), False

    branch_tip = git.branch_tip(issue_branch) if git.branch_exists(issue_branch) else ""
    resuming_own = bool(
        journal is not None
        and journal.issue_id == issue_id
        and journal.issue_branch == issue_branch
        and branch_tip
        and journal.branch_head
        and branch_tip == journal.branch_head
    )
    if branch_tip and not resuming_own and branch_tip != git.head_oid():
        return issue_branch, None, None, (
            f"branch {issue_branch} already exists at "
            f"{branch_tip[:12]}, not at the "
            f"integration head {git.head_oid()[:12]}; refusing "
            "to reuse or reset it — resolve the branch "
            "manually, then re-run grind"
        ), False

    workspace = repo / _WORKSPACES_DIR / issue_id
    if workspace.exists():
        if (
            journal is not None
            and journal.workspace_path
            and (repo / journal.workspace_path) == workspace
        ):
            # The startup sweep could not retire this workspace (it said why
            # in the log), so it may still hold the only copy of the work. A
            # claim never destroys that.
            return issue_branch, None, None, (
                f"the worker workspace at {journal.workspace_path} still "
                "holds unswept state; resolve the sweep failure recorded in "
                "the log, then re-run grind"
            ), False
        # Anything else is a leftover no journal owns; a fresh claim starts
        # from the primary's refs.
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    reason = git.clone_shared(git.head_oid(), workspace)
    if reason:
        return issue_branch, None, None, (
            f"could not materialize the worker workspace: {reason}"
        ), False
    workspace_git = _make_git(workspace)
    # Repo-local config does not clone: a primary whose committer identity
    # lives in .git/config would leave the workspace unable to commit at all.
    for key in ("user.name", "user.email"):
        value = git.config_value(key)
        if value:
            workspace_git.set_config(key, value)
    source = f"origin/{issue_branch}" if branch_tip else git.head_oid()
    if not workspace_git.create_branch(issue_branch, source):
        shutil.rmtree(workspace, ignore_errors=True)
        return issue_branch, None, None, (
            f"could not create {issue_branch} in the worker workspace"
        ), False
    checkout_reason = workspace_git.checkout_reporting(issue_branch)
    if checkout_reason:
        shutil.rmtree(workspace, ignore_errors=True)
        return issue_branch, None, None, (
            f"could not check out {issue_branch} in the worker "
            f"workspace: {checkout_reason}"
        ), False
    index = repo / ".codegraph"
    if index.is_dir() and not (workspace / ".codegraph").exists():
        try:
            # The primary's gitignore pattern (`.codegraph/`) matches only a
            # directory, not this symlink, so the clone excludes it at the
            # git level — a candidate must never absorb the index link
            # (observed live on ortus-k46v.1's first clone-mode run).
            exclude = workspace / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as fh:
                fh.write("/.codegraph\n")
            (workspace / ".codegraph").symlink_to(index.resolve())
        except OSError as exc:
            write_log(f"iter prep: could not link the CodeGraph index ({exc})")
    if resuming_own and journal is not None and (
        journal.base_head and journal.base_head != git.head_oid()
    ):
        # The integration branch advanced while this branch was parked. A
        # guard that rejects the stale fork point on every retry is a dead
        # end (ortus-ti4i's sibling), so the workspace carries the branch
        # forward: rebase onto the clone-time integration head, conflicts
        # reported as a claim blocker with the branch left untouched.
        rebase_reason = workspace_git.rebase_onto(git.head_oid(), issue_branch)
        if rebase_reason:
            shutil.rmtree(workspace, ignore_errors=True)
            return issue_branch, None, None, (
                f"{integration_branch} moved past {issue_branch}'s fork "
                f"point and the rebase forward hit a conflict: "
                f"{rebase_reason} — resolve the branch manually, then "
                "re-run grind"
            ), False
        write_log(
            f"iter prep: rebased parked {issue_branch} onto "
            f"{integration_branch} at {git.head_oid()[:12]} "
            f"(primary stays on {integration_branch})"
        )
        # A rebase is a re-cut: the fork point is new, so the caller
        # refreshes base_head and the capture recomputes the candidate
        # identity, exactly as for a fresh branch (ortus-ti4i).
        return issue_branch, workspace, workspace_git, "", False
    if resuming_own:
        write_log(
            f"iter prep: worker workspace resumed {issue_branch} at "
            f"{branch_tip[:12]} (primary stays on {integration_branch})"
        )
    else:
        write_log(
            f"iter prep: worker workspace on {issue_branch} at "
            f"{git.head_oid()[:12]} (primary stays on {integration_branch})"
        )
    return issue_branch, workspace, workspace_git, "", resuming_own


def _retire_workspace(
    git: GitClient,
    journal: CandidateJournal,
    *,
    repo: Path,
    write_log: Callable[[str], None],
    rescue_uncommitted: bool = False,
) -> CandidateJournal:
    """Bring the workspace's branch home and remove the clone.

    The clone is disposable; the primary repository's ref is the durable
    record. `rescue_uncommitted` additionally commits any dirty candidate
    work the clone still holds (a crashed run's tail) before fetching, so
    removal never destroys the only copy of anything.
    """

    if not journal.workspace_path:
        return journal
    workspace = repo / journal.workspace_path
    if not workspace.exists():
        return replace(journal, workspace_path="")
    if journal.issue_branch:
        workspace_git = _make_git(workspace)
        if workspace_git.branch_exists(journal.issue_branch):
            if rescue_uncommitted:
                dirty = workspace_git.dirty_paths() or frozenset()
                rescuable = frozenset(
                    path for path in dirty if not _is_tool_state(path)
                )
                if rescuable and workspace_git.current_branch() == (
                    journal.issue_branch
                ):
                    rescued = workspace_git.commit_paths(
                        rescuable,
                        f"{journal.issue_id}: capture uncommitted candidate "
                        "work\n\nPre-retirement rescue of a crashed "
                        "workspace's dirty paths.\n\nThe finalization pass "
                        "composes the durable message.",
                    )
                    if not rescued:
                        # Removal must never destroy the only copy of
                        # anything: an unrescuable workspace is kept whole.
                        write_log(
                            "workspace sweep: could not rescue uncommitted "
                            f"work ({rescued.reason}); the clone is kept for "
                            "manual recovery"
                        )
                        return journal
                    write_log(
                        "workspace sweep: rescued "
                        f"{len(rescuable)} uncommitted path(s) onto "
                        f"{journal.issue_branch}"
                    )
            primary_tip = git.branch_tip(journal.issue_branch)
            fetch_reason = git.fetch_branch(workspace, journal.issue_branch)
            if fetch_reason and primary_tip:
                # A capture-commit unwind or amend rewrote the harness's own
                # commits; the backup ref is replaced only if untouched.
                fetch_reason = git.replace_branch(
                    workspace, journal.issue_branch, expected_tip=primary_tip
                )
            if fetch_reason:
                write_log(
                    f"workspace sweep: could not fetch {journal.issue_branch} "
                    f"from {journal.workspace_path}: {fetch_reason}; the clone "
                    "is kept for manual recovery"
                )
                return journal
            journal = journal.with_branch(
                journal.issue_branch,
                git.branch_tip(journal.issue_branch) or journal.branch_head,
            )
    shutil.rmtree(workspace, ignore_errors=True)
    write_log(
        f"workspace sweep: {journal.workspace_path} removed; "
        + (
            f"{journal.issue_branch} preserved in the primary repository"
            if journal.issue_branch
            else "no branch to preserve"
        )
    )
    return replace(journal, workspace_path="")


#: Extra `journal.finalization` key (not a FINALIZATION_STEP): the issue-branch
#: SHA already pushed for merge-gate checks. Resume re-enters the wait without
#: pushing again. Kept out of the step vocabulary so gating-off journals stay
#: byte-identical to today's.
_GATE_PUSHED_KEY = "issue_branch_pushed"
_GATE_POLL_SECONDS = 15.0


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


def _apply_merge_gate(
    git: GitClient,
    store: JournalStore,
    journal: CandidateJournal,
    *,
    issue_branch: str,
    merge_gate: bool,
    merge_gate_timeout: float,
    write_log: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_interval: float = _GATE_POLL_SECONDS,
) -> tuple[CandidateJournal, str | None]:
    """Push the issue branch and wait for its checks when the gate is on.

    Returns ``(journal, None)`` when the caller may fast-forward, or a
    blocker naming the branch and the observed conclusion. Gating off is a
    silent no-op so every existing journal entry and log line is unchanged.
    """

    if not merge_gate:
        return journal, None
    if not issue_branch:
        return journal, None
    if not git.is_git_repo() or not git.has_remote():
        write_log("finalization: merge gate skipped (no remote)")
        return journal, None

    tip = git.branch_tip(issue_branch) or journal.branch_head
    already = journal.finalization.get(_GATE_PUSHED_KEY)
    if already != tip:
        if not _announced_push(git, issue_branch):
            return journal, (
                f"push of {issue_branch} to origin failed; the commit is "
                f"preserved on {issue_branch}"
            )
        record = dict(journal.finalization)
        record[_GATE_PUSHED_KEY] = tip
        journal = replace(journal, finalization=record)
        store.save(journal)
        write_log(
            f"finalization: pushed {issue_branch} at {tip[:12]} for merge-gate checks"
        )
    else:
        write_log(
            f"finalization: {issue_branch} already on origin at "
            f"{str(already)[:12]}; re-entering the check wait"
        )

    deadline = clock() + merge_gate_timeout
    write_log(
        f"finalization: waiting up to {int(merge_gate_timeout)}s for checks "
        f"on {issue_branch}"
    )
    output.progress(
        "grind",
        f"waiting for checks on {issue_branch} "
        f"(up to {int(merge_gate_timeout)}s)",
    )
    while True:
        conclusion = git.branch_checks(issue_branch)
        if conclusion == "success":
            write_log(f"finalization: checks on {issue_branch} passed")
            return journal, None
        if conclusion == "failure":
            return journal, (
                f"checks on {issue_branch} failed — the commit is preserved "
                f"on {issue_branch}"
            )
        remaining = deadline - clock()
        if remaining <= 0:
            return journal, (
                f"checks on {issue_branch} timed out after "
                f"{int(merge_gate_timeout)}s still pending — the commit is "
                f"preserved on {issue_branch}"
            )
        sleep(min(poll_interval, remaining))


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
    """Label when closed-count grew since spawn and HEAD is in sync.

    Predicted id does not matter: a worker that claimed a different ready
    issue still trips the bar. Missing origin tracking is not in sync.
    Tracker or git errors are None: a poll must not kill a live worker.
    """

    try:
        if not git.remote_tip(integration_branch):
            return None
        if git.local_ahead_of_remote(integration_branch) != 0:
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


def _fmt_duration(seconds: float) -> str:
    """Elapsed time the way a colleague would say it: `47s` under a minute,
    whole minutes after that. Console milestones only; the log keeps exact
    timestamps."""

    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m"


# ---------------------------------------------------------------------------
# Terminal-failure narration (ortus-ipyq). Every error the loop prints answers
# the operator's three questions — what happened, to which issue, and what to
# do now — and never states something the journal cannot back.
# ---------------------------------------------------------------------------

_NO_CHANGES = "no changes were made"

_RESUME_ACTION = (
    "run `ortus grind` again; it resumes this issue at verification "
    "with a fresh verifier"
)
_DECIDE_ACTION = (
    "read the issue's newest comment, decide, and relabel it for the queue"
)

#: Failure class → the operator's next action, keyed by the journal phase a
#: terminal failure leaves behind. The table lives beside the messages on
#: purpose: a new failure class must pick its action here — a halt with no
#: safe next action to name is a missing recovery design, not a wording
#: problem.
_NEXT_ACTION_BY_PHASE: dict[str, str] = {
    VERIFICATION_REJECTED: _RESUME_ACTION,
    VERIFICATION_TIMEOUT: _RESUME_ACTION,
    CORRECTION_TIMEOUT: _RESUME_ACTION,
    CORRECTION_REJECTED: _DECIDE_ACTION,
    CORRECTIONS_EXHAUSTED: _DECIDE_ACTION,
    PLAN_GAP_ROUTED: _DECIDE_ACTION,
    PLAN_GAP_ESCALATED: _DECIDE_ACTION,
    FINALIZATION_BLOCKED: (
        "resolve the blocker, then run `ortus grind` again; it resumes "
        "this exact issue"
    ),
}

#: Phases whose halt is a verdict on the candidate, so the narrative opens
#: with "verification of … failed" rather than the generic "work on … stopped".
_VERIFICATION_FAILURE_PHASES = frozenset(
    {VERIFICATION_REJECTED, VERIFICATION_TIMEOUT, CORRECTION_REJECTED}
)


def _issue_reference(issue_id: str, title: str = "") -> str:
    """`"<title>" (<id>)`, or the id alone when no title is readable."""

    collapsed = " ".join(str(title or "").split())
    return f'"{collapsed}" ({issue_id})' if collapsed else issue_id


def _issue_reference_from_bd(bd: BdClient, issue_id: str) -> str:
    """A title lookup must never break the error path it decorates."""

    try:
        return _issue_reference(issue_id, str(bd.show(issue_id).get("title") or ""))
    except Exception:
        return issue_id


def _candidate_state_phrase(git: GitClient, journal: CandidateJournal | None) -> str:
    """The candidate's whereabouts, computed from the journal and the repo.

    Never templated: a branch tip ahead of the candidate base means the work
    is committed on that branch; dirty candidate paths mean uncommitted edits
    survive in the tree; both mean both; neither means nothing was produced.
    """

    if journal is None:
        return _NO_CHANGES
    in_repo = git.is_git_repo()
    committed = bool(
        in_repo
        and journal.issue_branch
        and journal.base_head
        and git.branch_exists(journal.issue_branch)
        and git.branch_tip(journal.issue_branch) != journal.base_head
    )
    dirty = git.dirty_paths() if in_repo else None
    if dirty is None:
        # Status is unreadable; the journal's own capture is the best truth.
        preserved = bool(journal.candidate_paths) and not committed
    else:
        preserved = bool(frozenset(journal.candidate_paths) & dirty)
    if committed and preserved:
        return (
            f"committed on {journal.issue_branch}, with further uncommitted "
            "edits preserved in the tree"
        )
    if committed:
        return f"committed on {journal.issue_branch}"
    if preserved:
        return "uncommitted edits preserved in the tree"
    return _NO_CHANGES


def _safety_sentence(state: str, *, claim_kept: bool = False) -> str:
    """One sentence on the work's whereabouts, never softer than the truth."""

    if state == _NO_CHANGES:
        kept = ", and the claim is kept" if claim_kept else ""
        return f"No changes were made{kept}."
    kept = " — and the claim is kept" if claim_kept else ""
    return f"Its work is safe — {state}{kept}."


def _halt_narrative(
    *, issue_ref: str, phase: str, cause: str, state: str
) -> str:
    """The single terminal message for one halted issue: what happened, to
    which issue, where its work sits, and what the operator does next."""

    lead = (
        f"verification of {issue_ref} failed"
        if phase in _VERIFICATION_FAILURE_PHASES
        else f"work on {issue_ref} stopped"
    )
    action = _NEXT_ACTION_BY_PHASE.get(phase, _RESUME_ACTION)
    return (
        f"{lead}: {cause}.\n"
        f"{_safety_sentence(state, claim_kept=True)}\n"
        f"Next: {action}."
    )


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
            "claimed candidate for restart; Claude runs bd-state/orphan-policy "
            "recovery. 0 disables the watchdog (workers may then hang indefinitely)."
        ),
    ),
    max_corrections: int = typer.Option(
        0,
        "--max-corrections",
        help=(
            "Fresh implement+verify retries after a failed verdict "
            "(default 0: escalate immediately). Each attempt is a new worker "
            "with only the failed criteria and findings. Exhaustion flags the "
            "issue for a human and never merges."
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
        # Writing the commit message is bounded prose over material the pass is
        # handed, so it defaults to the cheap tier unless an operator says
        # otherwise in `.ortusrc`.
        finalize_profile = with_default_model(
            config.resolve_profile(resolved_backend, Phase.FINALIZE)
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
            write_log(f"phase profile: {implement_profile.display_name}")
            write_log(f"phase profile: {verify_profile.display_name}")
            write_log(f"phase profile: {finalize_profile.display_name}")
            # The commit-message model pass is retired (branch-scoped
            # candidates, commit B): the worker writes its message at commit
            # time and finalization repairs or replaces it deterministically.
            # The journal's compose step vocabulary survives until the
            # machine-verification deletion task so a legacy journal stranded
            # at finalized-compose still resumes; a None pass journals the
            # phase transition as unavailable and moves on.
            compose_message: ComposeCallable | None = None
            output.progress("grind", f"starting; log → {log.relative_to(target)}")

            bd = _make_bd(target)
            git = _make_git(target)
            if not git.is_git_repo():
                output.error("grind: working tree is not a git repository")
                raise typer.Exit(code=1)
            transaction_store = JournalStore(target)
            # Re-assert branch discipline before anything else: a stray branch
            # left by a prior crashed grind (or a manual checkout) is caught
            # here and either re-checked-out or halted on, so we never start
            # spawning workers on top of stranded work (ortus-6fu6). The one
            # sanctioned exception is the journal's own issue branch, whose
            # stranded commit the finalization replay below integrates.
            startup_journal = transaction_store.load()
            if startup_journal is not None and startup_journal.workspace_path:
                # A crashed run left its worker workspace behind. Rescue any
                # uncommitted tail onto the issue branch, bring the branch
                # home, and remove the clone — the resume below then works
                # from the primary repository's ref like any other.
                swept = _retire_workspace(
                    git,
                    startup_journal,
                    repo=target,
                    write_log=write_log,
                    rescue_uncommitted=True,
                )
                if swept is not startup_journal:
                    transaction_store.save(swept)
                    startup_journal = swept
            _enforce_branch_discipline(
                git,
                integration_branch,
                write_log,
                phase="startup",
            )
            # AC-6: a run killed between any two finalization phase transitions left a
            # journal that still owes work. Replay it BEFORE selecting anything,
            # and never select another issue while one is outstanding. Each step
            # re-checks observable bd/git state, so a replay of a phase transition that
            # actually landed is a no-op rather than a duplicate.
            pending_finalization = transaction_store.load()
            if (
                pending_finalization is not None
                and pending_finalization.phase in _FINALIZABLE_PHASES
            ):
                write_log(
                    "finalization resume: journal for "
                    f"{pending_finalization.issue_id} stopped at "
                    f"{pending_finalization.phase}; replaying remaining phase transitions"
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
                    compose=compose_message,
                    merge_gate=merge_gate,
                    merge_gate_timeout=merge_gate_timeout,
                )
                if blocker is not None:
                    write_log(f"finalization resume: HALT — {blocker}")
                    output.error(
                        _console_safe(
                            "could not finish the pending finalization of "
                            + _issue_reference_from_bd(
                                bd, pending_finalization.issue_id
                            )
                            + f" — {blocker}\n"
                            + _safety_sentence(
                                _candidate_state_phrase(git, pending_finalization)
                            )
                        ),
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
                # A journal resume names its issue directly, bypassing the
                # label filter every snapshot gate applies. Feeding an excluded
                # issue to a worker arms a trap: the worker runs, verification
                # cannot see the claim, and the finished candidate is silently
                # dropped (ortus-lf02). Skip the resume loudly instead — no
                # worker ever runs for a hidden claim; its journal, branch, and
                # claim stay parked, and the queue continues past it. The
                # transaction-handoff path retires its workspace when another
                # issue is claimed.
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
                        "dropped. Its claim, journal, and work stay parked; "
                        "read the issue's newest comment, decide, and relabel "
                        "it for the queue. The queue continues past it."
                    )
                    write_log(f"startup: {skip_note}")
                    output.warn(skip_note)
                    resume_issue_id = None
                    resume_candidate_ready = False

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
                candidate_git = git
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
                    git,
                    integration_branch,
                    write_log,
                    phase="pre-iter",
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
                    phase_profiles = {
                        "implementation": implement_profile.display_name,
                        "verification": verify_profile.display_name,
                        "finalization": finalize_profile.display_name,
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
                            if active_journal.workspace_path:
                                # The abandoned journal's clone may hold the
                                # only copy of its branch (rebases and amends
                                # happen in the clone; the primary ref lags).
                                # Retire it — rescue, fetch home, remove —
                                # before the new transaction buries the record
                                # that owned it (ortus-0wyq).
                                transaction_store.save(
                                    _retire_workspace(
                                        git,
                                        active_journal,
                                        repo=target,
                                        write_log=write_log,
                                        rescue_uncommitted=True,
                                    )
                                )
                        # The resume belongs to the inherited candidate, not to
                        # the run: a transaction starting here has no captured
                        # implementation by definition, so this issue gets a
                        # real implementation phase like any first claim.
                        resume_candidate_ready = False
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
                                f"to work spec {packet_digest}"
                            )
                        elif (
                            active_journal.issue_packet_hash != packet_digest
                            or active_journal.issue_packet_ref != packet_ref
                        ):
                            write_log(
                                "transaction handoff: work spec changed since the "
                                "prior worker; adopting the current authoritative work spec"
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
                    # Branch-scoped candidates (Phase L0) in a disposable
                    # workspace (ortus-u4zv.2): every claim materializes a
                    # shared clone on `ortus/<issue-id>`, cut at the
                    # integration head. The worker's commits accumulate there;
                    # finalization fetches the branch home. The primary
                    # checkout never leaves the integration branch, so
                    # operator intake can no longer collide with a candidate.
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
                    candidate_git = git
                    if active_journal is not None:
                        active_journal = replace(
                            active_journal,
                            issue_branch="",
                            workspace_path="",
                        )
                        transaction_store.save(active_journal)
                    dirty_after_claim = candidate_git.dirty_paths()
                    if dirty_after_claim is None:
                        output.error("grind: could not record candidate ownership")
                        raise typer.Exit(code=1)
                    active_journal = active_journal.with_candidate(
                        dirty_after_claim
                        - _candidate_baseline(active_journal, codex_baseline)
                        - _TRACKER_EXPORT_PATHS
                        - _TRACKER_TOOL_STATE,
                        phase=active_journal.phase
                        if resume_candidate_ready
                        else IMPLEMENTATION,
                    )
                    transaction_store.save(active_journal)
                    resume_issue_id = None
                    configure_codegraph = getattr(runner, "configure_codegraph", None)
                    if callable(configure_codegraph):
                        configure_codegraph(codegraph_probe.capability)
                    implementation_probe = codegraph_probe
                    if callable(configure_codegraph):
                        configure_codegraph(implementation_probe.capability)
                    implementation_instruction = _IMPLEMENTATION_INSTRUCTION
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
                    if resume_candidate_ready:
                        implementation_worker_ran = False
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
                if (
                    active_journal is not None
                    and active_journal.workspace_path
                ):
                    active_journal = _retire_workspace(
                        git,
                        active_journal,
                        repo=target,
                        write_log=write_log,
                        rescue_uncommitted=True,
                    )
                    transaction_store.save(active_journal)
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
            # The exit line accounts for unfinished work in words instead of
            # burying it in a status-count tuple: "awaiting retry" is a
            # claimed issue whose journal survived the run.
            # Checked against bd directly, not the label-filtered snapshot: an
            # escalated issue is excluded from the queue but its claim and
            # journal still hold unfinished work the operator must hear about.
            exit_journal = transaction_store.load()
            awaiting_retry = int(
                exit_journal is not None
                and bd.status(exit_journal.issue_id) == "in_progress"
            )
            output.progress(
                "grind",
                f"done — {tasks_completed} landed this session, "
                f"{awaiting_retry} awaiting retry, {final_snapshot.open} open",
            )
            if awaiting_retry and exit_journal is not None:
                output.progress(
                    "grind",
                    "next: "
                    + _NEXT_ACTION_BY_PHASE.get(exit_journal.phase, _RESUME_ACTION),
                )
    except FlockBusy as exc:
        output.error(str(exc), hint="another `ortus grind` is already running here")
        raise typer.Exit(code=1)
