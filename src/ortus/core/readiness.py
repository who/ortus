"""Mechanical Definition of Ready for executable Beads issues.

Readiness schema v1 deliberately lives in the existing Beads text fields.  It
is strict enough to keep architectural and product decisions out of the fast
implementation phase while remaining readable in ``bd show``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable


READINESS_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class ReadinessFailure:
    """One actionable defect in an implementation packet."""

    code: str
    field: str
    section: str
    message: str


@dataclass(frozen=True)
class ReadinessReport:
    """Structured validation result for one issue."""

    issue_id: str
    exempt: bool
    failures: tuple[ReadinessFailure, ...] = ()

    @property
    def ready(self) -> bool:
        return self.exempt or not self.failures

    def diagnostic(self) -> str:
        if self.ready:
            return f"{self.issue_id}: ready"
        details = "; ".join(
            f"{failure.field}/{failure.section}: {failure.message}"
            for failure in self.failures
        )
        return f"{self.issue_id}: {details}"


_REQUIRED_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("description", "objective", "objective"),
    ("description", "behavioral context", "behavioral_context"),
    ("design", "readiness schema", "readiness_schema"),
    ("design", "scope", "scope"),
    ("design", "non goals", "non_goals"),
    ("design", "concrete locations", "concrete_locations"),
    ("design", "resolved decisions", "resolved_decisions"),
    ("design", "compatibility constraints", "compatibility_constraints"),
    ("design", "ordered steps", "ordered_steps"),
    ("design", "dependencies", "dependencies"),
    ("design", "edge cases", "edge_cases"),
    ("design", "plan gap guidance", "plan_gap_guidance"),
    ("acceptance_criteria", "observable criteria", "observable_criteria"),
    ("acceptance_criteria", "criterion checks", "criterion_mapped_checks"),
    ("acceptance_criteria", "targeted tests", "targeted_tests"),
)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_PLACEHOLDER = re.compile(
    r"^(?:todo|tbd|placeholder|to be determined|fill (?:this )?in|unknown|n/?a|[-.]+)$",
    re.IGNORECASE,
)
_CRITERION_ID = re.compile(r"\bAC-\d+\b", re.IGNORECASE)
_ORDERED_STEP = re.compile(r"^\s*\d+[.)]\s+\S+", re.MULTILINE)
_CODE_SPAN = re.compile(r"`[^`\n]+`")
_TEST_COMMAND = re.compile(
    r"`[^`\n]*(?:pytest|test[_./:-]|unittest)[^`\n]*`", re.IGNORECASE
)
_LOCATION = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z0-9_-]+\.(?:py|md|toml|ya?ml|json|sh|ts|tsx|js|jsx|rs|go)"
)
_SYMBOL = re.compile(
    r"(?:\bclass\s+|\bfunction\s+|\bmethod\s+|\binterface\s+|::|\w+\.\w+|\w+\(\))",
    re.IGNORECASE,
)


def _normalise_heading(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _sections(value: Any) -> dict[str, str]:
    """Parse Markdown ATX headings without interpreting the section bodies."""

    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in str(value or "").splitlines():
        match = _HEADING.match(line)
        if match:
            current = _normalise_heading(match.group(1))
            found.setdefault(current, [])
        elif current is not None:
            found[current].append(line)
    return {heading: "\n".join(lines).strip() for heading, lines in found.items()}


def _is_placeholder(value: str) -> bool:
    stripped = value.strip().strip("`*_ ")
    if not stripped:
        return True
    meaningful_lines = []
    for line in stripped.splitlines():
        line = re.sub(r"^\s*(?:[-*+] |\d+[.)]\s+)", "", line).strip().strip("`*_ ")
        if line:
            meaningful_lines.append(line)
    return bool(meaningful_lines) and all(
        _PLACEHOLDER.fullmatch(line) or re.fullmatch(r"<[^>]+>", line)
        for line in meaningful_lines
    )


def _failure(code: str, field: str, section: str, message: str) -> ReadinessFailure:
    return ReadinessFailure(code, field, section, message)


def validate_issue(issue: dict[str, Any]) -> ReadinessReport:
    """Validate one issue against readiness schema v1; epics are containers."""

    issue_id = str(issue.get("id") or "<missing-id>").strip()
    issue_type = str(issue.get("issue_type") or issue.get("type") or "").strip().lower()
    if issue_type == "epic":
        return ReadinessReport(issue_id=issue_id, exempt=True)

    parsed = {
        field: _sections(issue.get(field))
        for field in {field for field, _, _ in _REQUIRED_SECTIONS}
    }
    failures: list[ReadinessFailure] = []
    values: dict[str, str] = {}
    for field, heading, code in _REQUIRED_SECTIONS:
        value = parsed[field].get(heading, "")
        values[code] = value
        if _is_placeholder(value):
            failures.append(
                _failure(
                    code,
                    field,
                    heading,
                    "missing, empty, or placeholder section",
                )
            )

    schema = values.get("readiness_schema", "").strip().lower()
    if schema and not _is_placeholder(schema) and schema != READINESS_SCHEMA_VERSION:
        failures.append(
            _failure(
                "readiness_schema",
                "design",
                "readiness schema",
                f"expected {READINESS_SCHEMA_VERSION!r}, got {schema!r}",
            )
        )

    locations = values.get("concrete_locations", "")
    if locations and not _is_placeholder(locations):
        if not _LOCATION.search(locations) or not _SYMBOL.search(locations):
            failures.append(
                _failure(
                    "concrete_locations",
                    "design",
                    "concrete locations",
                    "must name at least one file and one symbol or interface",
                )
            )

    steps = values.get("ordered_steps", "")
    if steps and not _is_placeholder(steps) and not _ORDERED_STEP.search(steps):
        failures.append(
            _failure(
                "ordered_steps",
                "design",
                "ordered steps",
                "must contain a numbered implementation step",
            )
        )

    criterion_counts = Counter(
        item.upper()
        for item in _CRITERION_ID.findall(values.get("observable_criteria", ""))
    )
    check_counts = Counter(
        item.upper()
        for item in _CRITERION_ID.findall(values.get("criterion_mapped_checks", ""))
    )
    criteria = set(criterion_counts)
    if values.get("observable_criteria") and not criteria:
        failures.append(
            _failure(
                "observable_criteria",
                "acceptance_criteria",
                "observable criteria",
                "each criterion must have an AC-N identifier",
            )
        )
    if values.get("criterion_mapped_checks") and (
        not criteria
        or set(check_counts) != criteria
        or any(count != 1 for count in criterion_counts.values())
        or any(count != 1 for count in check_counts.values())
        or not _CODE_SPAN.search(values["criterion_mapped_checks"])
    ):
        failures.append(
            _failure(
                "criterion_mapped_checks",
                "acceptance_criteria",
                "criterion checks",
                "must map every AC-N exactly once by identifier and include exact commands or checks",
            )
        )

    tests = values.get("targeted_tests", "")
    if tests and not _is_placeholder(tests) and not _TEST_COMMAND.search(tests):
        failures.append(
            _failure(
                "targeted_tests",
                "acceptance_criteria",
                "targeted tests",
                "must include an exact targeted test command in backticks",
            )
        )

    # A section can fail both presence and shape; collapse duplicate codes to
    # keep repair prompts and grind diagnostics bounded and actionable.
    unique: dict[str, ReadinessFailure] = {}
    for failure in failures:
        unique.setdefault(failure.code, failure)
    return ReadinessReport(
        issue_id=issue_id, exempt=False, failures=tuple(unique.values())
    )


def validate_issues(issues: Iterable[dict[str, Any]]) -> tuple[ReadinessReport, ...]:
    return tuple(validate_issue(issue) for issue in issues)


def failed_reports(reports: Iterable[ReadinessReport]) -> tuple[ReadinessReport, ...]:
    return tuple(report for report in reports if not report.ready)
