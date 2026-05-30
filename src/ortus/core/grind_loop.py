"""Outer-loop primitives for `ortus grind` subprocess-per-task shape (ortus-3ico).

The orchestration in `commands/grind.py` calls these to:
  - snapshot bd state before/after each claude subprocess,
  - compute the state delta and route to one of three branches
    (closed / orphan-claim / no-change),
  - apply the configured orphan-policy when a claimed-but-unclosed issue is
    detected,
  - decide whether the queue is drained and the loop should exit.

This module is the unit-test surface. The CLI command does IO; this module
does logic. bd state is treated as ground truth — model claims, `/goal`
evaluator judgments, and transcript sentinels are never consulted here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from importlib.resources import files
from typing import Iterable, Optional


CLOSE_ONE_CONDITION_FILE = "close-one.txt"
# Per-iteration condition for the harness-selects-and-claims flow: the loop
# picks + claims the next ready issue itself and injects its id + details here,
# so the worker never runs `bd ready` or transcribes a hash-like id (the
# id-hallucination failure mode that wedged workers until the watchdog killed
# them). The two placeholders below are substituted by inject_issue().
WORK_ISSUE_CONDITION_FILE = "work-issue.txt"
ISSUE_ID_PLACEHOLDER = "<ISSUE_ID>"
ISSUE_DETAILS_PLACEHOLDER = "<ISSUE_DETAILS>"
CONDITIONS_PACKAGE = "ortus.prompts.conditions"

# Labels whose presence on an issue makes it un-claimable by the agent loop.
# Applied to both the queue-drained check and the orphan-detection diff so
# escalated issues don't make the orchestrator spin (ortus-9db5). The
# close-one prompt mirrors this filter on its own `bd ready` call.
EXCLUDED_LABELS: tuple[str, ...] = ("human",)


# Default integration branch grind pins the working tree to. A closed issue's
# commit must land here (and be pushed to origin) to be deployable; a worker
# that drifts onto a feature branch (`git checkout -b ...`) commits there and
# leaves origin/main — where deploys come from — stale, silently stranding
# every "closed" issue off the deploy path (ortus-6fu6).
DEFAULT_INTEGRATION_BRANCH = "main"


class OrphanPolicy(str, enum.Enum):
    """How the outer loop reacts when an iteration leaves an issue claimed
    but not closed (the failure mode that left ortus-4q0m stale)."""

    WARN = "warn"
    REVERT = "revert"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class StateSnapshot:
    """Observable bd state at one instant: counts plus the set of
    in_progress issue ids (so orphan detection can name which issue was
    claimed during the iteration)."""

    closed: int
    in_progress: int
    open: int
    in_progress_ids: frozenset[str]

    @classmethod
    def from_counts(
        cls,
        *,
        closed: int,
        in_progress: int,
        open: int,
        in_progress_ids: Iterable[str] = (),
    ) -> "StateSnapshot":
        return cls(
            closed=closed,
            in_progress=in_progress,
            open=open,
            in_progress_ids=frozenset(in_progress_ids),
        )


@dataclass(frozen=True)
class StateDelta:
    """Difference between two StateSnapshots. The three signals the outer
    loop routes on:

      - closed_delta >= 1 → iteration succeeded; advance the task counter.
      - closed_delta == 0 AND in_progress_delta > 0 → orphan claim; honor policy.
      - closed_delta == 0 AND in_progress_delta == 0 → no-op iteration; idle-sleep.

    `orphan_ids` is the set of in_progress ids that appeared during the
    iteration (after - before). It is non-empty exactly when the orphan
    branch fires.
    """

    closed_delta: int
    in_progress_delta: int
    open_delta: int
    orphan_ids: frozenset[str]

    @property
    def closed_one_or_more(self) -> bool:
        return self.closed_delta >= 1

    @property
    def is_orphan(self) -> bool:
        return self.closed_delta == 0 and self.in_progress_delta > 0

    @property
    def is_no_change(self) -> bool:
        return self.closed_delta == 0 and self.in_progress_delta == 0


def compute_delta(before: StateSnapshot, after: StateSnapshot) -> StateDelta:
    """Pure: subtract counts; orphan_ids = (after.in_progress_ids - before.in_progress_ids)."""
    return StateDelta(
        closed_delta=after.closed - before.closed,
        in_progress_delta=after.in_progress - before.in_progress,
        open_delta=after.open - before.open,
        orphan_ids=frozenset(after.in_progress_ids - before.in_progress_ids),
    )


def queue_drained(snapshot: StateSnapshot) -> bool:
    """Outer loop exits when both `open` and `in_progress` counts are 0.

    Shell-side dumb-pipe check (acceptance #6): no model judgment involved.
    A drained queue is observable, not inferred.
    """
    return snapshot.open == 0 and snapshot.in_progress == 0


def read_close_one_condition() -> str:
    """Load the canonical per-task condition body shipped in the package.

    Mirrors `grind_logic._read_canonical` but for the close-one variant.
    """
    res = files(CONDITIONS_PACKAGE).joinpath(CLOSE_ONE_CONDITION_FILE)
    if not res.is_file():
        raise FileNotFoundError(
            f"close-one condition missing in {CONDITIONS_PACKAGE}"
        )
    text = res.read_text(encoding="utf-8")
    if text.lstrip().startswith("TODO PLACEHOLDER"):
        raise FileNotFoundError(
            "close-one condition is still a TODO placeholder"
        )
    return text


def read_work_issue_condition() -> str:
    """Load the per-iteration work-this-issue condition template.

    Mirrors :func:`read_close_one_condition` but for the variant whose issue
    SELECTION is done by the harness (not the worker). Still carries the two
    placeholders; call :func:`inject_issue` to fill them per iteration.
    """
    res = files(CONDITIONS_PACKAGE).joinpath(WORK_ISSUE_CONDITION_FILE)
    if not res.is_file():
        raise FileNotFoundError(
            f"work-issue condition missing in {CONDITIONS_PACKAGE}"
        )
    text = res.read_text(encoding="utf-8")
    if text.lstrip().startswith("TODO PLACEHOLDER"):
        raise FileNotFoundError(
            "work-issue condition is still a TODO placeholder"
        )
    return text


def select_ready_issue(ready: Iterable[dict]) -> Optional[dict]:
    """Pick the issue the harness should claim next: the first ready entry
    that is directly workable.

    `bd ready` already orders by priority (lowest number first), so we keep
    that ordering — we just move selection out of the worker (which
    mis-transcribed ids) and into the deterministic harness. Epics are SKIPPED:
    `bd ready` surfaces a parent epic alongside its ready children (and, being
    higher priority, often first), but an epic is a container, not a unit of
    work — a worker can't implement+close it, so claiming one would strand it
    in_progress while the worker closes a child instead. This mirrors the
    long-standing worker behavior (it always skipped epics in `bd ready`).
    Returns None when nothing workable is ready.
    """
    for issue in ready:
        if str(issue.get("issue_type") or issue.get("type") or "").strip() == "epic":
            continue
        return issue
    return None


def format_issue_details(issue: dict) -> str:
    """Render a ready-list/`bd show` issue dict into a compact human-readable
    block to inject into the worker prompt.

    Only fields that carry working context are included, and empty ones are
    dropped so a sparse issue doesn't produce a wall of blank labels. The id
    is intentionally NOT formatted here — it is injected separately (and
    repeatedly) by :func:`inject_issue` so the worker can't miss it.
    """
    lines: list[str] = []
    title = str(issue.get("title") or "").strip()
    if title:
        lines.append(f"Title: {title}")

    issue_type = str(issue.get("issue_type") or issue.get("type") or "").strip()
    if issue_type:
        lines.append(f"Type: {issue_type}")

    priority = issue.get("priority")
    if priority is not None:
        lines.append(f"Priority: {priority}")

    labels = issue.get("labels") or []
    if labels:
        lines.append("Labels: " + ", ".join(str(l) for l in labels))

    for field, heading in (
        ("description", "Description"),
        ("design", "Design"),
        ("acceptance_criteria", "Acceptance criteria"),
        ("notes", "Notes"),
    ):
        value = str(issue.get(field) or "").strip()
        if value:
            lines.append(f"\n{heading}:\n{value}")

    return "\n".join(lines).strip()


def inject_issue(template: str, issue: dict) -> str:
    """Substitute the issue id + rendered details into the work-issue template.

    Raises ValueError if the issue dict has no id — claiming/working an issue
    with no id is exactly the hallucination class this feature exists to kill,
    so we fail loud rather than emit a prompt with a dangling placeholder.
    """
    issue_id = str(issue.get("id") or "").strip()
    if not issue_id:
        raise ValueError("cannot inject issue with no id into work-issue prompt")
    details = format_issue_details(issue)
    return template.replace(ISSUE_ID_PLACEHOLDER, issue_id).replace(
        ISSUE_DETAILS_PLACEHOLDER, details
    )


@dataclass(frozen=True)
class OrphanAction:
    """What the outer loop did about an orphan claim. Logged + tested."""

    policy: OrphanPolicy
    orphan_ids: frozenset[str]
    actions_taken: tuple[str, ...]  # human-readable per-id action lines


def apply_orphan_policy(
    policy: OrphanPolicy,
    orphan_ids: Iterable[str],
    *,
    revert_fn=None,
    escalate_fn=None,
) -> OrphanAction:
    """Honor --orphan-policy for the set of issues claimed-but-not-closed.

    revert_fn(issue_id) and escalate_fn(issue_id) are injected so this
    module stays pure-logic; the CLI wires them to BdClient.update_status
    and BdClient.add_label respectively.
    """
    ids = frozenset(orphan_ids)
    if not ids:
        return OrphanAction(policy=policy, orphan_ids=ids, actions_taken=())

    if policy is OrphanPolicy.WARN:
        return OrphanAction(
            policy=policy,
            orphan_ids=ids,
            actions_taken=tuple(f"warn: orphan claim on {i}" for i in sorted(ids)),
        )

    if policy is OrphanPolicy.REVERT:
        if revert_fn is None:
            raise ValueError("orphan-policy=revert requires a revert_fn")
        taken: list[str] = []
        for issue_id in sorted(ids):
            revert_fn(issue_id)
            taken.append(f"revert: {issue_id} → open")
        return OrphanAction(
            policy=policy,
            orphan_ids=ids,
            actions_taken=tuple(taken),
        )

    if policy is OrphanPolicy.ESCALATE:
        if escalate_fn is None:
            raise ValueError("orphan-policy=escalate requires an escalate_fn")
        taken = []
        for issue_id in sorted(ids):
            escalate_fn(issue_id)
            taken.append(f"escalate: {issue_id} ← human label")
        return OrphanAction(
            policy=policy,
            orphan_ids=ids,
            actions_taken=tuple(taken),
        )

    raise ValueError(f"unknown orphan policy: {policy!r}")


# --- branch discipline (ortus-6fu6) ---------------------------------------
#
# grind workers commit + push the work that closes their issue. A worker that
# drifts onto a feature branch commits + pushes THAT branch and leaves
# origin/<integration> stale, so every "closed" issue sits off the deploy path
# and the operator keeps seeing supposedly-fixed bugs. The outer loop pins the
# working tree to the integration branch at the start of each iteration and
# re-verifies after a close that the commit actually reached origin. The git
# IO lives in core.git (GitClient); the classification below is pure logic so
# it stays on the unit-test surface alongside the rest of this module.


class BranchDisposition(str, enum.Enum):
    """What the loop should do about the observed git branch state.

      - OK        → on the integration branch and in sync with origin; proceed.
      - PUSH      → on the integration branch but local is ahead of origin;
                    push it (backstop for a worker that committed but didn't
                    push) and proceed.
      - REASSERT  → on a stray branch that carries NO commits absent from the
                    integration branch; safe to re-checkout the integration
                    branch and proceed (re-assert discipline, no work lost).
      - HALT      → stranded work detected (stray branch WITH unique commits,
                    or detached HEAD); surface loudly and stop rather than bury
                    the commits where deploys never see them.
    """

    OK = "ok"
    PUSH = "push"
    REASSERT = "reassert"
    HALT = "halt"


@dataclass(frozen=True)
class BranchState:
    """Observable git state the branch-discipline check routes on.

    `current_branch` is the checked-out branch name, or "" for a detached
    HEAD. `stray_commits` is the number of commits reachable from HEAD but not
    from the integration branch (0 when on the integration branch, or when a
    side branch has already been merged). `local_ahead_of_remote` is how many
    commits the integration branch is ahead of its origin tracking ref (0 when
    in sync, or when the remote/tracking ref can't be resolved — we never
    block on an indeterminate remote).
    """

    current_branch: str
    stray_commits: int
    local_ahead_of_remote: int
    integration_branch: str = DEFAULT_INTEGRATION_BRANCH


@dataclass(frozen=True)
class BranchDecision:
    """Result of classifying a BranchState: what to do + why (logged)."""

    disposition: BranchDisposition
    reason: str

    @property
    def should_halt(self) -> bool:
        return self.disposition is BranchDisposition.HALT


def classify_branch_state(state: BranchState) -> BranchDecision:
    """Pure: map a BranchState to a BranchDecision.

    The danger this exists to catch is a worker committing on a branch other
    than the integration branch and leaving origin/<integration> stale. We
    refuse to silently re-checkout away from a branch that carries unique
    commits (that would bury the work); instead we HALT and name the branch so
    the operator can fast-forward / cherry-pick it onto the integration branch.
    """
    branch = state.current_branch
    expected = state.integration_branch

    if not branch:
        return BranchDecision(
            BranchDisposition.HALT,
            "detached HEAD; refusing to proceed (any commit here would be "
            "unreachable from a branch and stranded off the deploy path)",
        )

    if branch != expected:
        if state.stray_commits > 0:
            return BranchDecision(
                BranchDisposition.HALT,
                f"on stray branch {branch!r} carrying {state.stray_commits} "
                f"commit(s) not on {expected!r}: work is stranded off the "
                f"deploy path. Fast-forward or cherry-pick {branch!r} onto "
                f"{expected!r} (and push origin/{expected}) before re-running grind.",
            )
        return BranchDecision(
            BranchDisposition.REASSERT,
            f"on stray branch {branch!r} with no commits absent from "
            f"{expected!r}; re-checking out {expected!r} to re-assert branch discipline",
        )

    # On the integration branch.
    if state.local_ahead_of_remote > 0:
        return BranchDecision(
            BranchDisposition.PUSH,
            f"on {expected!r} but {state.local_ahead_of_remote} commit(s) ahead "
            f"of origin/{expected}; pushing so the closed work is deployable",
        )
    return BranchDecision(
        BranchDisposition.OK,
        f"on {expected!r}, in sync with origin/{expected}",
    )
