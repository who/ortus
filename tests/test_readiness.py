from __future__ import annotations

from copy import deepcopy

from ortus.core.readiness import failed_reports, validate_issue, validate_issues


def ready_issue(issue_id: str = "demo-1") -> dict:
    return {
        "id": issue_id,
        "issue_type": "task",
        "description": """## Objective
Ship the bounded behavior.

## Behavioral context
The old path writes immediately; the new path can preview safely.""",
        "design": """## Readiness schema
v1

## Scope
Add and thread the preview flag.

## Non-goals
No output redesign.

## Concrete locations
Edit `src/demo.py` in `run()` and the `Executor.apply()` interface.

## Resolved decisions
Reuse the existing renderer.

## Compatibility constraints
Normal invocations remain unchanged.

## Ordered steps
1. Parse the flag.
2. Bypass writes.

## Dependencies
None — standalone; caller is `cli.run()`.

## Edge cases
Empty operation lists still succeed.

## Plan-gap guidance
If renderer ordering contradicts `Executor.apply()`, record PLAN-GAP and stop.""",
        "acceptance_criteria": """## Observable criteria
- AC-1: Preview performs no writes.
- AC-2: Normal execution is unchanged.

## Criterion checks
- AC-1: Run `uv run pytest tests/test_demo.py::test_preview -q`.
- AC-2: Run `uv run pytest tests/test_demo.py::test_run -q`.

## Targeted tests
Run `uv run pytest tests/test_demo.py -q`.""",
    }


def test_complete_leaf_is_ready() -> None:
    report = validate_issue(ready_issue())
    assert report.ready
    assert not report.exempt
    assert report.failures == ()


def test_incomplete_leaf_reports_every_required_surface() -> None:
    report = validate_issue({"id": "legacy-1", "issue_type": "task"})
    codes = {failure.code for failure in report.failures}
    assert {
        "scope",
        "non_goals",
        "concrete_locations",
        "resolved_decisions",
        "ordered_steps",
        "dependencies",
        "edge_cases",
        "criterion_mapped_checks",
        "targeted_tests",
    } <= codes
    assert not report.ready


def test_placeholder_and_unmapped_checks_are_rejected() -> None:
    issue = ready_issue()
    issue["design"] = issue["design"].replace("No output redesign.", "TBD")
    issue["acceptance_criteria"] = issue["acceptance_criteria"].replace(
        "- AC-2: Run `uv run pytest tests/test_demo.py::test_run -q`.\n", ""
    )
    codes = {failure.code for failure in validate_issue(issue).failures}
    assert "non_goals" in codes
    assert "criterion_mapped_checks" in codes


def test_epic_is_exempt_and_mixed_graph_only_fails_bad_leaf() -> None:
    epic = {"id": "demo-e", "issue_type": "epic", "description": "broad"}
    bad = {"id": "demo-bad", "issue_type": "bug"}
    reports = validate_issues([epic, ready_issue(), bad])
    assert reports[0].ready and reports[0].exempt
    assert [report.issue_id for report in failed_reports(reports)] == ["demo-bad"]


def test_contradiction_guidance_must_be_actionable() -> None:
    issue = deepcopy(ready_issue())
    issue["design"] = issue["design"].replace(
        "If renderer ordering contradicts `Executor.apply()`, record PLAN-GAP and stop.",
        "TODO",
    )
    report = validate_issue(issue)
    assert "plan_gap_guidance" in {failure.code for failure in report.failures}
