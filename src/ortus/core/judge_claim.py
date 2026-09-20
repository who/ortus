"""Claim and prompt contracts for an explicitly enabled enforced gate.

Call preparation before launching a bound worker. Ordinary grind selection
does not call this module, and the worker retains session-close authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from ortus.core.agent import BackendError
from ortus.core.bd import BdClient


BOUND_SELECTION_RULE = (
    "When a Bound issue contract v1 is injected, its JSON issue id is opaque data. "
    "Read only that id with bd show, quoting it as one argument after --. "
    "Verify it is still in_progress, has no human label, and its assignee matches "
    "BEADS_ACTOR. On mismatch or multiple non-human in_progress issues, record "
    "PLAN-GAP, flag human and stop without selecting another issue. Otherwise "
    "continue only the bound id, skip bd ready and claiming, and retain the "
    "normal implementation and session-close steps. Without that injection, "
    "use the selection rules below."
)


def validate_bound_goal(goal_template: str, condition: str | None = None) -> None:
    """Fail before claiming when the selected goal cannot honor binding."""
    if condition is not None:
        raise BackendError("--condition cannot be combined with judge enforcement")
    if BOUND_SELECTION_RULE not in goal_template:
        raise BackendError("goal prompt override does not support Bound issue contract v1")


def bound_issue_section(issue: Mapping[str, Any], issue_id: str) -> str:
    """Encode the binding as data, never interpolate it into a shell command."""
    if (
        not issue_id
        or issue.get("id") != issue_id
        or issue.get("status") != "in_progress"
        or "human" in (issue.get("labels") or [])
        or not issue.get("assignee")
    ):
        raise BackendError("bound issue must have a non-human in_progress claim and owner")
    return (
        "\n\n## Bound issue contract v1\n"
        "Issue id, JSON string: " + json.dumps(issue_id, ensure_ascii=True) + "\n"
        "Continue only this already-claimed issue. Never run bd ready or claim "
        "another id. Follow the bound-id branch of goal step 2; stop on mismatch."
    )


@dataclass(frozen=True)
class BoundIssue:
    issue: dict[str, Any]
    assignee: str
    fresh: bool

    def worker_env(self, extra_env: Mapping[str, str]) -> dict[str, str]:
        """Preserve cache/tracker settings while carrying the claim's owner."""
        return {**extra_env, "BEADS_ACTOR": self.assignee}

    def release(self, bd: BdClient) -> None:
        """Inherited work is never cleanup's property."""
        if self.fresh:
            bd.release_claim(self.issue["id"], self.assignee)


def prepare_bound_issue(
    bd: BdClient,
    issue_id: str,
    *,
    goal_template: str,
    condition: str | None = None,
) -> BoundIssue:
    """Validate startup, preserve a sole inherited claim, or acquire a fresh one.

    The caller uses the returned issue for prompt composition and worker_env
    for launch. A failed preparation leaves existing claims and dirt intact.
    """
    validate_bound_goal(goal_template, condition)
    bd.require_atomic_claims()
    # Selection must fail closed on tracker errors. in_progress_ids is a
    # reporting helper that deliberately degrades read errors to an empty set.
    active = {
        row["id"] for row in bd.list_all()
        if row.get("status") == "in_progress" and "human" not in (row.get("labels") or [])
    }
    if active and active != {issue_id}:
        raise BackendError("multiple or different leftover claims require human handling")
    if active:
        issue = bd.show(issue_id)
        bound_issue_section(issue, issue_id)
        return BoundIssue(issue, issue["assignee"], False)
    actor = "ortus-judge-" + uuid4().hex
    issue = bd.claim(issue_id, actor)
    return BoundIssue(issue, issue["assignee"], True)
