from __future__ import annotations

import re
from copy import deepcopy

import pytest

from ortus.core.readiness import (
    _REQUIRED_SECTIONS,
    RequiredSection,
    failed_reports,
    spec_markdown,
    validate_issue,
    validate_issues,
)


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


def test_full_miss_collapses_to_no_packet() -> None:
    """AC-1: an empty packet has one problem, not fifteen."""
    report = validate_issue({"id": "legacy-1", "issue_type": "task"})
    total = len(_REQUIRED_SECTIONS)
    assert report.packet_missing
    assert report.summary() == (
        f"no readiness packet ({total} of {total} sections missing)"
    )


def test_partial_miss_names_only_failures() -> None:
    """AC-2: the summary names failing sections only; one failure drops the
    count clause; passing sections are never mentioned."""
    issue = ready_issue()
    issue["design"] = issue["design"].replace(
        "## Non-goals\nNo output redesign.\n\n", ""
    )
    issue["design"] = issue["design"].replace(
        "1. Parse the flag.\n2. Bypass writes.", "Parse, then bypass."
    )
    report = validate_issue(issue)
    total = len(_REQUIRED_SECTIONS)
    assert not report.packet_missing
    assert report.summary() == (
        f"failing sections (2 of {total}): design/non goals, design/ordered steps"
    )
    assert "design/scope" not in report.summary()

    single = ready_issue()
    single["design"] = single["design"].replace(
        "1. Parse the flag.\n2. Bypass writes.", "Parse, then bypass."
    )
    assert validate_issue(single).summary() == (
        "failing section: design/ordered steps"
    )


def test_epic_is_exempt_and_mixed_graph_only_fails_bad_leaf() -> None:
    epic = {"id": "demo-e", "issue_type": "epic", "description": "broad"}
    bad = {"id": "demo-bad", "issue_type": "bug"}
    reports = validate_issues([epic, ready_issue(), bad])
    assert reports[0].ready and reports[0].exempt
    assert [report.issue_id for report in failed_reports(reports)] == ["demo-bad"]


_SPEC_BULLET = re.compile(r"^- `## (?P<heading>[^`]+)` — ", re.MULTILINE)
_SPEC_FIELD = re.compile(r"^`(?P<field>[a-z_]+)`:$", re.MULTILINE)

# Bodies that satisfy the shape rules the rendered spec teaches; every other
# section only needs concrete, non-placeholder prose.
_SPEC_BODIES = {
    "readiness schema": "v1",
    "concrete locations": "Edit `src/demo.py` in `run()`.",
    "ordered steps": "1. Parse the flag.\n2. Suppress writes.",
    "observable criteria": "- AC-1: Preview performs no writes.",
    "criterion checks": "- AC-1: Run `uv run pytest tests/test_demo.py -q`.",
    "targeted tests": "Run `uv run pytest tests/test_demo.py -q`.",
}


def _packet_from_spec(rendered: str) -> dict:
    """Author an issue by following the rendered contract, heading by heading."""

    issue: dict[str, str] = {"id": "spec-1", "issue_type": "task"}
    section_list = rendered.split("### Shape rules")[0]
    field: str | None = None
    for line in section_list.splitlines():
        field_match = _SPEC_FIELD.match(line)
        bullet = _SPEC_BULLET.match(line)
        if field_match:
            field = field_match.group("field")
        elif bullet and field:
            heading = bullet.group("heading")
            body = _SPEC_BODIES.get(heading.lower(), "Concrete, decided detail.")
            issue[field] = f"{issue.get(field, '')}\n\n## {heading}\n{body}".strip()
    return issue


def test_spec_markdown_names_every_field_and_heading_in_validator_order() -> None:
    rendered = spec_markdown()
    section_list = rendered.split("### Shape rules")[0]
    assert _SPEC_BULLET.findall(section_list) == [
        section.heading for section in _REQUIRED_SECTIONS
    ]
    assert _SPEC_FIELD.findall(section_list) == list(
        dict.fromkeys(section.field for section in _REQUIRED_SECTIONS)
    )
    assert spec_markdown() == rendered  # stable across runs, no churn


def test_failures_report_normalised_headings_not_display_headings() -> None:
    # grind diagnostics and the repair prompt match these strings; attaching
    # display headings to the section table must not reword them.
    report = validate_issue({"id": "old-1", "issue_type": "task", "description": "do it"})
    sections = {failure.section for failure in report.failures}
    assert {"scope", "non goals", "plan gap guidance"} <= sections
    assert "design/scope" in report.diagnostic()
    assert not any(section != section.lower() for section in sections)


def test_spec_markdown_records_the_epic_exemption() -> None:
    # validate_issue() exempts epics; authors pad them needlessly if the
    # rendered contract stays silent about it.
    assert "epic" in spec_markdown().lower()


def test_rendered_spec_round_trip_passes_validation() -> None:
    report = validate_issue(_packet_from_spec(spec_markdown()))
    assert report.failures == ()
    assert report.ready


def test_section_without_guidance_fails_loudly() -> None:
    with pytest.raises(ValueError, match="guidance"):
        RequiredSection("design", "Scope", "scope", "  ")


def test_kind_tags_are_accepted_not_required() -> None:
    """AC-5 (l2u9.2): tagged and untagged criterion lines both validate, so
    every pre-existing (untagged) packet keeps validating exactly as before."""
    untagged = validate_issue(ready_issue())
    assert untagged.ready and untagged.failures == ()

    tagged = ready_issue("demo-tagged")
    tagged["acceptance_criteria"] = (
        tagged["acceptance_criteria"]
        .replace(
            "- AC-1: Preview performs no writes.",
            "- AC-1 (proves-new): Preview performs no writes.",
        )
        .replace(
            "- AC-2: Normal execution is unchanged.",
            "- AC-2 (guards-existing): Normal execution is unchanged.",
        )
    )
    report = validate_issue(tagged)
    assert report.ready and report.failures == ()


def test_contradiction_guidance_must_be_actionable() -> None:
    issue = deepcopy(ready_issue())
    issue["design"] = issue["design"].replace(
        "If renderer ordering contradicts `Executor.apply()`, record PLAN-GAP and stop.",
        "TODO",
    )
    report = validate_issue(issue)
    assert "plan_gap_guidance" in {failure.code for failure in report.failures}
