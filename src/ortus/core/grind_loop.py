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
CONDITIONS_PACKAGE = "ortus.prompts.conditions"

# Labels whose presence on an issue makes it un-claimable by the agent loop.
# Applied to both the queue-drained check and the orphan-detection diff so
# escalated issues don't make the orchestrator spin (ortus-9db5). The
# close-one prompt mirrors this filter on its own `bd ready` call.
EXCLUDED_LABELS: tuple[str, ...] = ("human",)


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
