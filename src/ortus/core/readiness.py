"""Mechanical Definition of Ready for executable Beads issues.

Readiness schema v1 deliberately lives in the existing Beads text fields.  It
is strict enough to keep architectural and product decisions out of the fast
implementation phase while remaining readable in ``bd show``.
"""

from __future__ import annotations

import re
import shlex
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable


READINESS_SCHEMA_VERSION = "v1"

# Stable key for the bd memory that points sessions at the contract. bd updates
# a memory with a matching key in place, so re-seeding it never duplicates.
READINESS_MEMORY_KEY = "ortus-readiness-contract"


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


@dataclass(frozen=True)
class RequiredSection:
    """One required heading plus the guidance that teaches how to fill it."""

    field: str
    heading: str
    code: str
    guidance: str

    def __post_init__(self) -> None:
        # A section added without guidance would render an empty bullet in the
        # generated contract; fail at import instead of teaching nothing.
        if not self.guidance.strip():
            raise ValueError(f"required section {self.code!r} has no guidance")

    @property
    def key(self) -> str:
        """Normalised heading used to match a parsed section."""

        return _normalise_heading(self.heading)


_REQUIRED_SECTIONS: tuple[RequiredSection, ...] = (
    RequiredSection(
        "description",
        "Objective",
        "objective",
        "the single outcome this leaf owns.",
    ),
    RequiredSection(
        "description",
        "Behavioral context",
        "behavioral_context",
        "user-visible or system behavior before and after.",
    ),
    RequiredSection(
        "design",
        "Readiness schema",
        "readiness_schema",
        f"exactly `{READINESS_SCHEMA_VERSION}`.",
    ),
    RequiredSection(
        "design",
        "Scope",
        "scope",
        "work included in this leaf.",
    ),
    RequiredSection(
        "design",
        "Non-goals",
        "non_goals",
        "explicit boundaries.",
    ),
    RequiredSection(
        "design",
        "Concrete locations",
        "concrete_locations",
        "candidate files plus symbols, interfaces, or commands; use CodeGraph "
        "evidence or record the grep/Read fallback.",
    ),
    RequiredSection(
        "design",
        "Resolved decisions",
        "resolved_decisions",
        "architectural and product decisions already made, including rationale "
        "where useful.",
    ),
    RequiredSection(
        "design",
        "Compatibility constraints",
        "compatibility_constraints",
        "supported platforms, APIs, stored data, CLI behavior, or an explained "
        "absence.",
    ),
    RequiredSection(
        "design",
        "Ordered steps",
        "ordered_steps",
        "a numbered implementation sequence.",
    ),
    RequiredSection(
        "design",
        "Dependencies",
        "dependencies",
        "issue dependencies plus code callers/consumers, or an explained absence.",
    ),
    RequiredSection(
        "design",
        "Edge cases",
        "edge_cases",
        "failures and boundary conditions the implementation must cover.",
    ),
    RequiredSection(
        "design",
        "Plan-gap guidance",
        "plan_gap_guidance",
        "contradictions or missing material decisions that require the worker "
        "to stop, record `PLAN-GAP`, preserve candidate state, and route to "
        "planning/human handling instead of improvising.",
    ),
    RequiredSection(
        "acceptance_criteria",
        "Observable criteria",
        "observable_criteria",
        "one observable result per stable identifier.",
    ),
    RequiredSection(
        "acceptance_criteria",
        "Criterion checks",
        "criterion_mapped_checks",
        "exactly one matching entry for every criterion identifier, with an "
        "exact command or deterministic inspection in backticks.",
    ),
    RequiredSection(
        "acceptance_criteria",
        "Targeted tests",
        "targeted_tests",
        "exact bounded test commands in backticks. Follow the repository's "
        "testing policy; do not make a full local matrix the worker default.",
    ),
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


def section_text(value: Any, heading: str) -> str:
    """Body of one Markdown section of `value`, or "" when it has no such heading.

    Exposed because the packet's authored sections are useful outside readiness
    validation — the finalization commit message quotes the objective — and a
    second heading parser would drift from the one that validates the packet.
    """

    return _sections(value).get(_normalise_heading(heading), "")


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


def _section(code: str) -> RequiredSection:
    for section in _REQUIRED_SECTIONS:
        if section.code == code:
            return section
    raise KeyError(f"unknown readiness section {code!r}")


def _shape_failure(code: str, message: str) -> ReadinessFailure:
    """A defect in a section that is present but malformed."""

    section = _section(code)
    # Diagnostics report the normalised heading, not the display one, so the
    # strings grind and the repair prompt already match stay byte-identical.
    return _failure(code, section.field, section.key, message)


def validate_issue(issue: dict[str, Any]) -> ReadinessReport:
    """Validate one issue against readiness schema v1; epics are containers."""

    issue_id = str(issue.get("id") or "<missing-id>").strip()
    issue_type = str(issue.get("issue_type") or issue.get("type") or "").strip().lower()
    if issue_type == "epic":
        return ReadinessReport(issue_id=issue_id, exempt=True)

    parsed = {
        field: _sections(issue.get(field))
        for field in {section.field for section in _REQUIRED_SECTIONS}
    }
    failures: list[ReadinessFailure] = []
    values: dict[str, str] = {}
    for section in _REQUIRED_SECTIONS:
        value = parsed[section.field].get(section.key, "")
        values[section.code] = value
        if _is_placeholder(value):
            failures.append(
                _failure(
                    section.code,
                    section.field,
                    section.key,
                    "missing, empty, or placeholder section",
                )
            )

    schema = values.get("readiness_schema", "").strip().lower()
    if schema and not _is_placeholder(schema) and schema != READINESS_SCHEMA_VERSION:
        failures.append(
            _shape_failure(
                "readiness_schema",
                f"expected {READINESS_SCHEMA_VERSION!r}, got {schema!r}",
            )
        )

    locations = values.get("concrete_locations", "")
    if locations and not _is_placeholder(locations):
        if not _LOCATION.search(locations) or not _SYMBOL.search(locations):
            failures.append(
                _shape_failure(
                    "concrete_locations",
                    "must name at least one file and one symbol or interface",
                )
            )

    steps = values.get("ordered_steps", "")
    if steps and not _is_placeholder(steps) and not _ORDERED_STEP.search(steps):
        failures.append(
            _shape_failure(
                "ordered_steps",
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
            _shape_failure(
                "observable_criteria",
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
            _shape_failure(
                "criterion_mapped_checks",
                "must map every AC-N exactly once by identifier and include exact commands or checks",
            )
        )

    tests = values.get("targeted_tests", "")
    if tests and not _is_placeholder(tests) and not _TEST_COMMAND.search(tests):
        failures.append(
            _shape_failure(
                "targeted_tests",
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


def _example(pattern: re.Pattern[str], example: str) -> str:
    """An illustrative token, proven against the rule it teaches."""

    if not pattern.search(example):
        raise ValueError(f"spec example {example!r} violates {pattern.pattern!r}")
    return example


def _shape_rules() -> tuple[str, ...]:
    """Rules that a present section must satisfy, keyed to the enforcing regex."""

    return (
        f"`## {_section('readiness_schema').heading}` — the body is exactly "
        f"`{READINESS_SCHEMA_VERSION}` and nothing else.",
        f"`## {_section('concrete_locations').heading}` — names at least one "
        f"file path ({_example(_LOCATION, '`src/pkg/module.py`')}) and at least "
        f"one symbol or interface ({_example(_SYMBOL, '`Runner.apply()`')}).",
        f"`## {_section('ordered_steps').heading}` — numbered steps, one per "
        f"line ({_example(_ORDERED_STEP, '1. Parse the flag.')}).",
        f"`## {_section('observable_criteria').heading}` — every criterion "
        f"carries a unique identifier ({_example(_CRITERION_ID, '`AC-1`')}, "
        "`AC-2`, ...).",
        f"`## {_section('criterion_mapped_checks').heading}` — repeats every "
        "criterion identifier exactly once, each with an exact command or "
        "deterministic check in backticks "
        f"({_example(_CODE_SPAN, '`uv run pytest tests/test_demo.py::test_x -q`')}).",
        f"`## {_section('targeted_tests').heading}` — at least one exact test "
        "command in backticks "
        f"({_example(_TEST_COMMAND, '`uv run pytest tests/test_demo.py -q`')}).",
    )


def spec_markdown() -> str:
    """Render readiness schema v1 exactly as ``validate_issue`` enforces it."""

    lines = [
        f"## Readiness schema {READINESS_SCHEMA_VERSION} for executable leaves",
        "",
        "Use these exact Markdown headings inside the existing bd fields. Every "
        "section must contain concrete information. `TODO`, `TBD`, `N/A`, an "
        "empty heading, and template text are invalid. When something is "
        "intentionally absent, write `None — <why that is safe>`.",
    ]
    fields: list[str] = []
    for section in _REQUIRED_SECTIONS:
        if section.field not in fields:
            fields.append(section.field)
    for field in fields:
        lines.extend(("", f"`{field}`:", ""))
        lines.extend(
            f"- `## {section.heading}` — {section.guidance}"
            for section in _REQUIRED_SECTIONS
            if section.field == field
        )
    lines.extend(("", "### Shape rules", ""))
    lines.extend(f"- {rule}" for rule in _shape_rules())
    lines.extend(
        (
            "",
            "Epics are containers and are exempt from these sections; every "
            "non-epic issue must satisfy all of them. Notes may carry "
            "supplementary evidence only, never required readiness content.",
        )
    )
    return "\n".join(lines) + "\n"


def readiness_memory_text() -> str:
    """Render the pointer bd injects into every ``bd prime``.

    Deliberately a pointer rather than the contract itself: memories are
    reloaded in every session and after every compaction, so the body names
    the verb that prints the sections instead of restating them.
    """
    return (
        f"Ortus readiness schema {READINESS_SCHEMA_VERSION}: every non-epic bd "
        "issue must carry the required headings in its description, design and "
        "acceptance criteria, or `ortus grind` skips it as unready. Run "
        "`ortus spec` for the full contract."
    )


def readiness_memory_command() -> str:
    """The exact `bd remember` invocation that seeds the pointer memory."""
    return (
        f"bd remember {shlex.quote(readiness_memory_text())} "
        f"--key {READINESS_MEMORY_KEY}"
    )


def validate_issues(issues: Iterable[dict[str, Any]]) -> tuple[ReadinessReport, ...]:
    return tuple(validate_issue(issue) for issue in issues)


def failed_reports(reports: Iterable[ReadinessReport]) -> tuple[ReadinessReport, ...]:
    return tuple(report for report in reports if not report.ready)
