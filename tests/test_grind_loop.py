"""Unit tests for ortus.core.grind_loop (ortus-3ico pivot).

These pin the pure-logic surface of the new subprocess-per-task outer
loop: state snapshots, delta computation, queue-drained check, and the
orphan-policy dispatcher.
"""

from __future__ import annotations

import pytest

from ortus.core.grind_loop import (
    CLOSE_ONE_CONDITION_FILE,
    EXCLUDED_LABELS,
    OrphanPolicy,
    StateSnapshot,
    apply_orphan_policy,
    compute_delta,
    queue_drained,
    read_close_one_condition,
)


# --- StateSnapshot / compute_delta -----------------------------------------


def test_compute_delta_closed_branch() -> None:
    before = StateSnapshot.from_counts(closed=5, in_progress=1, open=3, in_progress_ids=["a"])
    after = StateSnapshot.from_counts(closed=6, in_progress=0, open=3, in_progress_ids=[])
    delta = compute_delta(before, after)
    assert delta.closed_delta == 1
    assert delta.in_progress_delta == -1
    assert delta.closed_one_or_more
    assert not delta.is_orphan
    assert not delta.is_no_change
    assert delta.orphan_ids == frozenset()


def test_compute_delta_orphan_branch() -> None:
    """Subprocess claimed an issue but didn't close it: closed_delta=0, in_progress_delta>0."""
    before = StateSnapshot.from_counts(closed=5, in_progress=0, open=3, in_progress_ids=[])
    after = StateSnapshot.from_counts(closed=5, in_progress=1, open=2, in_progress_ids=["x"])
    delta = compute_delta(before, after)
    assert delta.closed_delta == 0
    assert delta.in_progress_delta == 1
    assert not delta.closed_one_or_more
    assert delta.is_orphan
    assert not delta.is_no_change
    assert delta.orphan_ids == frozenset(["x"])


def test_compute_delta_no_change_branch() -> None:
    """Subprocess exited cleanly without affecting bd state at all."""
    before = StateSnapshot.from_counts(closed=5, in_progress=0, open=3)
    after = StateSnapshot.from_counts(closed=5, in_progress=0, open=3)
    delta = compute_delta(before, after)
    assert delta.closed_delta == 0
    assert delta.in_progress_delta == 0
    assert not delta.closed_one_or_more
    assert not delta.is_orphan
    assert delta.is_no_change
    assert delta.orphan_ids == frozenset()


def test_compute_delta_orphan_ids_isolates_new_claims() -> None:
    """When before already has an in_progress, orphan_ids should only flag
    the NEW one — not the pre-existing claim."""
    before = StateSnapshot.from_counts(
        closed=5, in_progress=1, open=3, in_progress_ids=["pre-existing"]
    )
    after = StateSnapshot.from_counts(
        closed=5, in_progress=2, open=2, in_progress_ids=["pre-existing", "new"]
    )
    delta = compute_delta(before, after)
    assert delta.orphan_ids == frozenset(["new"])
    assert delta.is_orphan


def test_compute_delta_close_and_claim_in_same_iter_counts_as_closed() -> None:
    """If an iteration closed one and claimed another, closed_delta>=1 wins
    over in_progress_delta>0. Acceptance #4: orphan branch fires ONLY when
    closed_delta == 0."""
    before = StateSnapshot.from_counts(closed=5, in_progress=0, open=3, in_progress_ids=[])
    after = StateSnapshot.from_counts(closed=6, in_progress=1, open=1, in_progress_ids=["y"])
    delta = compute_delta(before, after)
    assert delta.closed_one_or_more
    assert not delta.is_orphan


# --- queue_drained --------------------------------------------------------


def test_queue_drained_when_both_zero() -> None:
    snapshot = StateSnapshot.from_counts(closed=100, in_progress=0, open=0)
    assert queue_drained(snapshot)


def test_queue_not_drained_when_open_pending() -> None:
    snapshot = StateSnapshot.from_counts(closed=100, in_progress=0, open=1)
    assert not queue_drained(snapshot)


def test_queue_not_drained_when_in_progress_pending() -> None:
    snapshot = StateSnapshot.from_counts(closed=100, in_progress=1, open=0)
    assert not queue_drained(snapshot)


# --- close-one condition --------------------------------------------------


def test_close_one_condition_is_packaged() -> None:
    body = read_close_one_condition()
    assert body.strip(), "close-one.txt should not be empty"
    # Acceptance #2: the per-iteration condition is NARROW — close exactly
    # one issue, NOT drive the queue to zero. The "you are done when ..."
    # contract pins this scope.
    lowered = body.lower()
    assert "close exactly one" in lowered, (
        "close-one condition should mandate exactly-one scope"
    )
    # The condition must NOT instruct the model to drive the queue to zero.
    # Look for the queue-zero contract phrase rather than the literal
    # substring "queue to zero", which appears in our explanatory text
    # explaining that the model is NOT doing it.
    assert "drive the bd queue to zero" not in lowered, (
        "close-one condition should not include the queue-zero contract"
    )


def test_close_one_condition_file_constant_matches_filename() -> None:
    assert CLOSE_ONE_CONDITION_FILE == "close-one.txt"


def test_excluded_labels_includes_human() -> None:
    """The orchestrator's snapshot filter must skip human-flagged issues
    so escalations stop the spin loop (ortus-9db5)."""
    assert "human" in EXCLUDED_LABELS


def test_close_one_condition_excludes_human_label_from_bd_ready() -> None:
    """Issues labeled 'human' must be filtered out of the ready queue (ortus-9db5).

    Orphan-policy ESCALATE adds the 'human' label to claims the agent couldn't
    complete. Without --exclude-label=human, grind sessions keep re-picking
    those issues, re-verifying, and exiting without progress.
    """
    body = read_close_one_condition()
    # Every `bd ready` invocation in the condition must carry the filter.
    import re

    invocations = re.findall(r"bd ready[^\n`]*", body)
    assert invocations, "close-one.txt should reference `bd ready`"
    for inv in invocations:
        assert "--exclude-label=human" in inv, (
            f"`bd ready` invocation in close-one.txt is missing "
            f"--exclude-label=human: {inv!r}"
        )


# --- apply_orphan_policy --------------------------------------------------


def test_apply_orphan_policy_warn_takes_no_action() -> None:
    revert_calls: list[str] = []
    escalate_calls: list[str] = []
    action = apply_orphan_policy(
        OrphanPolicy.WARN,
        ["a", "b"],
        revert_fn=lambda i: revert_calls.append(i),
        escalate_fn=lambda i: escalate_calls.append(i),
    )
    assert revert_calls == []
    assert escalate_calls == []
    assert action.policy is OrphanPolicy.WARN
    assert action.orphan_ids == frozenset(["a", "b"])
    assert len(action.actions_taken) == 2
    assert all("warn" in line for line in action.actions_taken)


def test_apply_orphan_policy_revert_calls_revert_fn() -> None:
    calls: list[str] = []
    action = apply_orphan_policy(
        OrphanPolicy.REVERT,
        ["alpha", "beta"],
        revert_fn=lambda i: calls.append(i),
    )
    # Sorted iteration order so action lines are deterministic.
    assert calls == ["alpha", "beta"]
    assert action.policy is OrphanPolicy.REVERT
    assert "alpha" in action.actions_taken[0]
    assert "beta" in action.actions_taken[1]


def test_apply_orphan_policy_escalate_calls_escalate_fn() -> None:
    calls: list[str] = []
    action = apply_orphan_policy(
        OrphanPolicy.ESCALATE,
        ["zz", "aa"],
        escalate_fn=lambda i: calls.append(i),
    )
    # Sorted iteration order.
    assert calls == ["aa", "zz"]
    assert action.policy is OrphanPolicy.ESCALATE
    assert all("human" in line for line in action.actions_taken)


def test_apply_orphan_policy_empty_ids_short_circuits() -> None:
    """No-op when there are no orphans — no callback invocations."""
    revert_calls: list[str] = []
    escalate_calls: list[str] = []
    action = apply_orphan_policy(
        OrphanPolicy.REVERT,
        [],
        revert_fn=lambda i: revert_calls.append(i),
        escalate_fn=lambda i: escalate_calls.append(i),
    )
    assert revert_calls == []
    assert escalate_calls == []
    assert action.actions_taken == ()


def test_apply_orphan_policy_revert_requires_callback() -> None:
    with pytest.raises(ValueError, match="revert"):
        apply_orphan_policy(OrphanPolicy.REVERT, ["x"])


def test_apply_orphan_policy_escalate_requires_callback() -> None:
    with pytest.raises(ValueError, match="escalate"):
        apply_orphan_policy(OrphanPolicy.ESCALATE, ["x"])
