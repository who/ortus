"""Unit tests for ortus.core.grind_loop (ortus-3ico pivot).

These pin the pure-logic surface of the new subprocess-per-task outer
loop: state snapshots, delta computation, queue-drained check, and the
orphan-policy dispatcher.
"""

from __future__ import annotations

from importlib.resources import files

import pytest

from ortus.core.grind_loop import (
    CONDITIONS_PACKAGE,
    DEFAULT_INTEGRATION_BRANCH,
    EXCLUDED_LABELS,
    ISSUE_DETAILS_PLACEHOLDER,
    ISSUE_ID_PLACEHOLDER,
    WORK_ISSUE_CONDITION_FILE,
    BranchDisposition,
    BranchState,
    OrphanPolicy,
    StateSnapshot,
    apply_orphan_policy,
    classify_branch_state,
    compute_delta,
    epic_is_exhausted,
    format_issue_details,
    inject_issue,
    queue_drained,
    read_work_issue_condition,
    select_ready_issue,
)
from tests.test_readiness import ready_issue


# --- StateSnapshot / compute_delta -----------------------------------------


def test_compute_delta_closed_branch() -> None:
    before = StateSnapshot.from_counts(
        closed=5, in_progress=1, open=3, in_progress_ids=["a"]
    )
    after = StateSnapshot.from_counts(
        closed=6, in_progress=0, open=3, in_progress_ids=[]
    )
    delta = compute_delta(before, after)
    assert delta.closed_delta == 1
    assert delta.in_progress_delta == -1
    assert delta.closed_one_or_more
    assert not delta.is_orphan
    assert not delta.is_no_change
    assert delta.orphan_ids == frozenset()


def test_compute_delta_orphan_branch() -> None:
    """Subprocess claimed an issue but didn't close it: closed_delta=0, in_progress_delta>0."""
    before = StateSnapshot.from_counts(
        closed=5, in_progress=0, open=3, in_progress_ids=[]
    )
    after = StateSnapshot.from_counts(
        closed=5, in_progress=1, open=2, in_progress_ids=["x"]
    )
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
    before = StateSnapshot.from_counts(
        closed=5, in_progress=0, open=3, in_progress_ids=[]
    )
    after = StateSnapshot.from_counts(
        closed=6, in_progress=1, open=1, in_progress_ids=["y"]
    )
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


# --- worker condition lifecycle contract ----------------------------------


def test_work_issue_condition_is_packaged() -> None:
    body = read_work_issue_condition()
    assert body.strip(), "work-issue.txt should not be empty"
    # The per-iteration condition is NARROW — one harness-claimed issue, NOT
    # driving the queue to zero.
    assert "drive the bd queue to zero" not in body.lower(), (
        "work-issue condition should not include the queue-zero contract"
    )


def test_work_issue_condition_forbids_agent_owned_lifecycle_mutations() -> None:
    """No shipped condition may tell a worker to close, commit, or push.

    Ortus alone performs those steps, and only after a passing verdict bound
    to the current candidate hash. A condition that re-granted them to the
    worker would let a candidate land with no verification at all — the exact
    failure the candidate transaction exists to prevent.
    """
    body = read_work_issue_condition()
    for forbidden in ("bd close", "git commit", "git push", "git stash", "git reset"):
        assert f"`{forbidden}`" in body, (
            f"work-issue condition must name {forbidden!r} in its prohibition list"
        )
    lowered = body.lower()
    assert "do not run" in lowered
    assert "owns closing, committing, and pushing" in lowered


def test_close_one_condition_is_retired() -> None:
    """The legacy self-closing condition must stay deleted (ortus-pzfd.5).

    `close-one.txt` instructed the agent to close the issue and then commit
    and push it. Nothing reads it since Ortus took ownership of finalization,
    but a shipped asset granting those permissions is one `--condition` away
    from bypassing verification entirely.
    """
    assert not files(CONDITIONS_PACKAGE).joinpath("close-one.txt").is_file()


def test_excluded_labels_includes_human() -> None:
    """The orchestrator's snapshot filter must skip human-flagged issues
    so escalations stop the spin loop (ortus-9db5)."""
    assert "human" in EXCLUDED_LABELS


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


# --- harness-select / work-issue injection (ortus-xo1u) -------------------


def test_work_issue_condition_is_packaged_and_has_placeholders() -> None:
    body = read_work_issue_condition()
    assert body.strip(), "work-issue.txt should not be empty"
    # The harness fills these per iteration; they MUST survive in the template.
    assert ISSUE_ID_PLACEHOLDER in body
    assert ISSUE_DETAILS_PLACEHOLDER in body
    # The worker must be told NOT to self-select / re-derive ids — that's the
    # whole point of moving selection into the harness.
    lowered = body.lower()
    assert "bd ready" in lowered, "work-issue.txt should mention not running bd ready"
    assert "PLAN-GAP" in body
    assert "bd comments add <ISSUE_ID>" in body
    assert "do not commit, discard, or close" in lowered
    assert WORK_ISSUE_CONDITION_FILE == "work-issue.txt"


def test_select_ready_issue_takes_first() -> None:
    ready = [ready_issue("p-1"), ready_issue("p-2"), ready_issue("p-3")]
    assert select_ready_issue(ready) == ready[0]


def test_select_ready_issue_empty_returns_none() -> None:
    assert select_ready_issue([]) is None


def test_select_ready_issue_skips_epics() -> None:
    """`bd ready` surfaces a parent epic (often first, being higher priority)
    alongside its ready children. An epic isn't directly workable, so the
    harness must skip it and claim the first real task — else it strands the
    epic in_progress while the worker closes a child instead."""
    ready = [
        {"id": "e-1", "issue_type": "epic", "priority": 1},
        {**ready_issue("t-1"), "priority": 2},
    ]
    assert select_ready_issue(ready) == ready[1]


def test_select_ready_issue_all_epics_returns_none() -> None:
    """If every ready entry is an epic, there's nothing workable to claim."""
    assert select_ready_issue([{"id": "e-1", "issue_type": "epic"}]) is None


def test_select_ready_issue_skips_legacy_leaf_with_exact_diagnostics() -> None:
    legacy = {"id": "old-1", "issue_type": "task", "description": "do it"}
    current = ready_issue("new-1")
    seen = []
    assert (
        select_ready_issue(
            [legacy, current],
            on_unready=lambda issue, report: seen.append((issue, report)),
        )
        == current
    )
    assert seen[0][0] == legacy
    assert "design/scope" in seen[0][1].diagnostic()


def test_mixed_ready_queue_returns_the_ready_leaf_in_either_order() -> None:
    """ortus-xhrj.7 AC-4: a queue holding one ready and one unready leaf is
    claimable as-is, so grind must never spend repair budget on it.

    The selector stays pure — it just returns the ready leaf — and grind only
    reaches its repair path when the selector returns None. Both orderings
    matter because `bd ready` sorts by priority, so the unready leaf can come
    first.
    """
    unready = {"id": "legacy-1", "issue_type": "task", "description": "do it"}
    ready = ready_issue("new-1")

    for queue in ([unready, ready], [ready, unready]):
        assert select_ready_issue(queue) == ready

    # An epic in the same queue is skipped silently: it is exempt from
    # readiness, so it must never be reported as repairable.
    epic = {"id": "e-1", "issue_type": "epic", "priority": 0}
    reported: list[str] = []
    assert (
        select_ready_issue(
            [epic, unready, ready],
            on_unready=lambda issue, report: reported.append(report.issue_id),
        )
        == ready
    )
    assert reported == ["legacy-1"]


def test_epic_is_exhausted_all_children_closed() -> None:
    epic = {
        "id": "e-1",
        "issue_type": "epic",
        "dependents": [
            {"id": "e-1.1", "dependency_type": "parent-child", "status": "closed"},
            {"id": "e-1.2", "dependency_type": "parent-child", "status": "closed"},
            # A blocks-dependent (the next milestone) must not count as a child.
            {"id": "e-2", "dependency_type": "blocks", "status": "open"},
        ],
    }
    assert epic_is_exhausted(epic)


def test_epic_is_exhausted_open_child_or_childless_or_non_epic() -> None:
    open_child = {
        "id": "e-1",
        "issue_type": "epic",
        "dependents": [
            {"id": "e-1.1", "dependency_type": "parent-child", "status": "closed"},
            {"id": "e-1.2", "dependency_type": "parent-child", "status": "open"},
        ],
    }
    assert not epic_is_exhausted(open_child)
    # Childless epic: possibly not yet decomposed — never auto-close.
    assert not epic_is_exhausted({"id": "e-3", "issue_type": "epic"})
    # Non-epics are never exhausted, whatever their dependents look like.
    assert not epic_is_exhausted(
        {
            "id": "t-1",
            "issue_type": "task",
            "dependents": [
                {"id": "x", "dependency_type": "parent-child", "status": "closed"}
            ],
        }
    )


def test_format_issue_details_includes_core_fields() -> None:
    issue = {
        "id": "p-9",
        "title": "Add widget",
        "issue_type": "feature",
        "priority": 1,
        "labels": ["grind", "reliability"],
        "description": "Why the widget matters.",
        "acceptance_criteria": "DONE WHEN the widget works.",
    }
    details = format_issue_details(issue)
    assert "Title: Add widget" in details
    assert "Type: feature" in details
    assert "Priority: 1" in details
    assert "Labels: grind, reliability" in details
    assert "Why the widget matters." in details
    assert "DONE WHEN the widget works." in details
    # The id is injected separately (repeatedly) — not part of the details block.
    assert "p-9" not in details


def test_format_issue_details_drops_empty_fields() -> None:
    issue = {"id": "p-2", "title": "Bare", "description": "", "design": None}
    details = format_issue_details(issue)
    assert details == "Title: Bare"


def test_inject_issue_substitutes_id_and_details() -> None:
    template = (
        f"Work {ISSUE_ID_PLACEHOLDER}. Details:\n{ISSUE_DETAILS_PLACEHOLDER}\n"
        f"Close {ISSUE_ID_PLACEHOLDER}."
    )
    issue = {"id": "ortus-abcd", "title": "Do the thing"}
    out = inject_issue(template, issue)
    assert ISSUE_ID_PLACEHOLDER not in out
    assert ISSUE_DETAILS_PLACEHOLDER not in out
    # The exact id appears everywhere the placeholder was (worker can't miss it).
    assert out.count("ortus-abcd") == 2
    assert "Title: Do the thing" in out


def test_inject_issue_rejects_missing_id() -> None:
    with pytest.raises(ValueError, match="no id"):
        inject_issue("Work <ISSUE_ID>.", {"title": "no id here"})


def test_inject_real_template_yields_no_placeholders() -> None:
    """End-to-end: the bundled template fully resolves for a realistic issue."""
    issue = {
        "id": "ortus-xo1u",
        "title": "grind: select + claim in-harness",
        "issue_type": "feature",
        "priority": 1,
        "description": "Workers invent non-existent bd ids.",
        "acceptance_criteria": "DONE WHEN harness selects+claims+injects.",
    }
    out = inject_issue(read_work_issue_condition(), issue)
    assert ISSUE_ID_PLACEHOLDER not in out
    assert ISSUE_DETAILS_PLACEHOLDER not in out
    assert "ortus-xo1u" in out


# --- branch discipline (ortus-6fu6) ---------------------------------------


def _branch_state(
    current: str,
    *,
    stray: int = 0,
    ahead: int = 0,
    integration: str = "main",
) -> BranchState:
    return BranchState(
        current_branch=current,
        stray_commits=stray,
        local_ahead_of_remote=ahead,
        integration_branch=integration,
    )


def test_default_integration_branch_is_main() -> None:
    assert DEFAULT_INTEGRATION_BRANCH == "main"


def test_classify_on_integration_in_sync_is_ok() -> None:
    decision = classify_branch_state(_branch_state("main"))
    assert decision.disposition is BranchDisposition.OK
    assert not decision.should_halt


def test_classify_on_integration_ahead_of_remote_is_push() -> None:
    """Worker committed the close onto main but didn't push it: push backstop."""
    decision = classify_branch_state(_branch_state("main", ahead=2))
    assert decision.disposition is BranchDisposition.PUSH
    assert not decision.should_halt
    assert "ahead" in decision.reason.lower()


def test_classify_stray_branch_no_unique_commits_is_reassert() -> None:
    """On a side branch that carries nothing past main → safe to re-checkout."""
    decision = classify_branch_state(_branch_state("feature-x", stray=0))
    assert decision.disposition is BranchDisposition.REASSERT
    assert not decision.should_halt
    assert "feature-x" in decision.reason


def test_classify_stray_branch_with_commits_halts() -> None:
    """The core ortus-6fu6 failure: work committed on a side branch is stranded.

    grind must HALT and name the branch, not silently re-checkout main (which
    would bury the commits off the deploy path)."""
    decision = classify_branch_state(_branch_state("prdpad-01v-clerk-dark", stray=12))
    assert decision.disposition is BranchDisposition.HALT
    assert decision.should_halt
    assert "prdpad-01v-clerk-dark" in decision.reason
    assert "12" in decision.reason


def test_classify_detached_head_halts() -> None:
    """Detached HEAD ('' branch): a commit here is unreachable; refuse to run."""
    decision = classify_branch_state(_branch_state(""))
    assert decision.disposition is BranchDisposition.HALT
    assert decision.should_halt
    assert "detached" in decision.reason.lower()


def test_classify_honors_custom_integration_branch() -> None:
    """When the integration branch is something other than main, being ON it
    is OK and being on main is then a stray branch."""
    on_release = classify_branch_state(_branch_state("release", integration="release"))
    assert on_release.disposition is BranchDisposition.OK
    on_main = classify_branch_state(
        _branch_state("main", stray=3, integration="release")
    )
    assert on_main.disposition is BranchDisposition.HALT
