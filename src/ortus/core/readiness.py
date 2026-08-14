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

# The presence-failure message, shared with ``ReadinessReport.packet_missing``
# so "every section absent" is recognised by identity, not string guesswork.
_MISSING_SECTION_MESSAGE = "missing, empty, or placeholder section"


@dataclass(frozen=True)
class ReadinessFailure:
    """One actionable defect in an implementation work spec."""

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

    @property
    def packet_missing(self) -> bool:
        """True when the issue carries no readiness work spec at all."""

        codes = {failure.code for failure in self.failures}
        return (
            not self.exempt
            and codes == {section.code for section in _required_sections()}
            and all(
                failure.message == _MISSING_SECTION_MESSAGE
                for failure in self.failures
            )
        )

    def summary(self) -> str:
        """One clause at console altitude; ``diagnostic()`` keeps the detail.

        An empty work spec has one problem, not fifteen, so a full miss collapses
        to a count against the schema total. A partial miss names only the
        failing sections; a single failing section skips the count clause.
        """

        if self.ready:
            return "ready"
        total = len(_REQUIRED_SECTIONS)
        if self.packet_missing:
            required = len(_required_sections())
            return (
                f"no readiness work spec ({required} of {required} sections missing)"
            )
        sections = [
            f"{failure.field}/{failure.section}" for failure in self.failures
        ]
        if len(sections) == 1:
            return f"failing section: {sections[0]}"
        return (
            f"failing sections ({len(sections)} of {total}): "
            + ", ".join(sections)
        )


@dataclass(frozen=True)
class RequiredSection:
    """One schema heading plus the guidance that teaches how to fill it."""

    field: str
    heading: str
    code: str
    guidance: str
    required: bool = True

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
        "the single outcome this task owns.",
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
        "work included in this task.",
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
        "one observable result per stable identifier; each line may carry "
        "one command (backticks optional).",
    ),
    RequiredSection(
        "acceptance_criteria",
        "Criterion checks",
        "criterion_mapped_checks",
        "optional alias: exactly one matching entry for every criterion "
        "identifier, with one runnable command (backticks optional). Omit "
        "when each Observable line already carries its command.",
        required=False,
    ),
    RequiredSection(
        "acceptance_criteria",
        "Targeted tests",
        "targeted_tests",
        "optional. Omit, write `None — <why>`, or give exact bounded test "
        "commands (backticks optional). Follow the repository's testing "
        "policy; do not make a full local matrix the worker default.",
        required=False,
    ),
)


def _required_sections() -> tuple[RequiredSection, ...]:
    return tuple(section for section in _REQUIRED_SECTIONS if section.required)


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_PLACEHOLDER = re.compile(
    r"^(?:todo|tbd|placeholder|to be determined|fill (?:this )?in|unknown|n/?a|[-.]+)$",
    re.IGNORECASE,
)
_CRITERION_ID = re.compile(r"\bAC-\d+\b", re.IGNORECASE)
#: The optional criterion kinds the red–green proof recognises. The tag rides
#: the Observable-criteria line (`- AC-3 (proves-new): ...`) — accepted here,
#: never required: an untagged criterion keeps its branch-only meaning, so
#: every work spec that validated before kinds existed validates identically.
CRITERION_KIND_PROVES_NEW = "proves-new"
CRITERION_KIND_GUARDS_EXISTING = "guards-existing"
_CRITERION_KIND = re.compile(
    rf"\b(AC-\d+)\s*\(\s*"
    rf"({CRITERION_KIND_PROVES_NEW}|{CRITERION_KIND_GUARDS_EXISTING})"
    rf"\s*\)",
    re.IGNORECASE,
)
_ORDERED_STEP = re.compile(r"^\s*\d+[.)]\s+\S+", re.MULTILINE)
_CODE_SPAN = re.compile(r"`[^`\n]+`")
_TEST_INVOCATION = re.compile(
    r"(?:pytest|unittest|python\s+-m\s+pytest)\b", re.IGNORECASE
)
_COMMAND_LEAD_IN = re.compile(
    r"^(?:run|execute|check|verify|invoke)\b[:\s]*", re.IGNORECASE
)
_COMMAND_RUNNERS = frozenset(
    {
        "uv",
        "python",
        "python3",
        "pytest",
        "rg",
        "git",
        "bd",
        "gh",
        "make",
        "cargo",
        "go",
        "npm",
        "pnpm",
        "yarn",
        "node",
        "ruff",
        "mypy",
        "tox",
        "nox",
        "true",
        "false",
        "echo",
        "sleep",
        "seq",
        "sh",
        "bash",
        "env",
        "ortus",
        "test",
        "touch",
        "cat",
        "ls",
        "cmp",
        "diff",
        "wc",
        "head",
        "tail",
        "mkdir",
        "rm",
        "cp",
        "mv",
    }
)
_MISSING_COMMAND = "no runnable command"
_AMBIGUOUS_COMMAND = "ambiguous: more than one command-looking span"
_LOCATION = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z0-9_-]+\.(?:py|md|toml|ya?ml|json|sh|ts|tsx|js|jsx|rs|go)"
)
_SYMBOL = re.compile(
    r"(?:\bclass\s+|\bfunction\s+|\bmethod\s+|\binterface\s+|::|\w+\.\w+|\w+\(\))",
    re.IGNORECASE,
)


def _normalise_heading(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _first_token(text: str) -> str:
    stripped = text.strip()
    return stripped.split(None, 1)[0] if stripped else ""


def _normalize_command(text: str) -> str:
    cleaned = _COMMAND_LEAD_IN.sub("", text.strip()).strip()
    return cleaned.rstrip(".").strip()


def looks_like_command(text: str) -> bool:
    """True when `text` starts with a known runner after optional lead-in prose."""

    token = _first_token(_normalize_command(text))
    return token.rsplit("/", 1)[-1] in _COMMAND_RUNNERS


def _check_line_body(line: str, criterion_id: str) -> str:
    match = re.search(rf"\b{re.escape(criterion_id)}\b", line, re.IGNORECASE)
    rest = line[match.end() :] if match else line
    rest = re.sub(r"^\s*\([^)]*\)", "", rest)
    return re.sub(r"^\s*[:.-]\s*", "", rest).strip()


def extract_check_command(line: str, criterion_id: str) -> tuple[str | None, str | None]:
    """Return ``(command, error)`` for one Criterion-checks or Observable line.

    Prefers the unique command-looking backtick span. If none, uses the rest
    of the line after the AC-N label when that remainder is a command.
    Two command-looking spans are ambiguous. Prose-only lines have no command.
    """

    body = _check_line_body(line, criterion_id)
    command_spans = [
        span[1:-1].strip()
        for span in _CODE_SPAN.findall(body)
        if looks_like_command(span[1:-1])
    ]
    if len(command_spans) > 1:
        return None, _AMBIGUOUS_COMMAND
    if len(command_spans) == 1:
        return _normalize_command(command_spans[0]), None
    unwrapped = _CODE_SPAN.sub(lambda match: match.group(0)[1:-1], body)
    if looks_like_command(unwrapped):
        return _normalize_command(unwrapped), None
    return None, _MISSING_COMMAND


def targeted_test_command(section: str) -> str | None:
    """The first test invocation in a Targeted tests section, backticks optional."""

    for line in section.splitlines():
        command, error = extract_check_command(f"- AC-0: {line}", "AC-0")
        if error is None and command is not None and _TEST_INVOCATION.search(command):
            return command
    command, error = extract_check_command(
        f"- AC-0: {section.replace(chr(10), ' ')}", "AC-0"
    )
    if error is None and command is not None and _TEST_INVOCATION.search(command):
        return command
    return None


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

    Exposed because the work spec's authored sections are useful outside readiness
    validation — the finalization commit message quotes the objective — and a
    second heading parser would drift from the one that validates the work spec.
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


_EXPLAINED_ABSENCE = re.compile(r"^None\s*[—–-]+\s+\S")


def _is_explained_absence(value: str) -> bool:
    """True for the contract's `None — <why that is safe>` form."""

    lines = [
        re.sub(r"^\s*(?:[-*+] |\d+[.)]\s+)", "", line).strip().strip("`*_ ")
        for line in value.splitlines()
    ]
    meaningful = [line for line in lines if line]
    return bool(meaningful) and all(_EXPLAINED_ABSENCE.match(line) for line in meaningful)


def _has_section_content(value: str) -> bool:
    return bool(value) and not _is_placeholder(value) and not _is_explained_absence(value)


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
        if section.required and _is_placeholder(value):
            failures.append(
                _failure(
                    section.code,
                    section.field,
                    section.key,
                    _MISSING_SECTION_MESSAGE,
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
    has_mapped_checks = _has_section_content(values.get("criterion_mapped_checks", ""))
    if has_mapped_checks and (
        not criteria
        or set(check_counts) != criteria
        or any(count != 1 for count in criterion_counts.values())
        or any(count != 1 for count in check_counts.values())
    ):
        failures.append(
            _shape_failure(
                "criterion_mapped_checks",
                "must map every AC-N exactly once by identifier and include exact commands or checks",
            )
        )
    elif criteria:
        # The machine pipeline executes each check with the claim-time parser,
        # so its rules ARE readiness: a check the parser refuses (for example a
        # criterion carrying more than one backticked command) would pass here
        # only to fail every claim as a work-spec defect. checks.py imports
        # this module for section parsing, so the parser is imported at call
        # time rather than at module level. When Criterion checks is omitted,
        # the same parser reads commands from the Observable lines.
        from ortus.core.checks import parse_criterion_checks

        parsed_checks, packet_failures = parse_criterion_checks(
            issue.get("acceptance_criteria")
        )
        fail_code = (
            "criterion_mapped_checks" if has_mapped_checks else "observable_criteria"
        )
        if packet_failures:
            for packet_failure in packet_failures:
                failures.append(_shape_failure(fail_code, packet_failure.message))
        elif {check.criterion_id for check in parsed_checks} != criteria:
            failures.append(
                _shape_failure(
                    fail_code,
                    "each criterion must carry one runnable command when "
                    "Criterion checks is omitted"
                    if not has_mapped_checks
                    else "must map every AC-N exactly once by identifier and include exact commands or checks",
                )
            )

    tests = values.get("targeted_tests", "")
    if (
        _has_section_content(tests)
        and not targeted_test_command(tests)
    ):
        failures.append(
            _shape_failure(
                "targeted_tests",
                "must include a targeted test command (backticks optional)",
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
        "`AC-2`, ...). When Criterion checks is omitted, each line also "
        "carries one runnable command (backticks optional).",
        f"`## {_section('criterion_mapped_checks').heading}` — optional. When "
        "present, repeats every criterion identifier exactly once, each with "
        "one runnable command (backticks optional) "
        f"({_example(_CODE_SPAN, '`uv run pytest tests/test_demo.py::test_x -q`')}). "
        "A command-looking span wins among several; extra spans that are not "
        "commands (version pins, paths) are ignored. Two command-looking "
        "spans on one criterion are ambiguous and fail readiness.",
        f"`## {_section('targeted_tests').heading}` — optional. Omit it, write "
        "`None — <why>`, or include at least one test invocation (backticks "
        "optional) "
        f"({_example(_TEST_INVOCATION, 'uv run pytest tests/test_demo.py -q')}).",
    )


def spec_markdown() -> str:
    """Render readiness schema v1 exactly as ``validate_issue`` enforces it."""

    lines = [
        f"## Readiness schema {READINESS_SCHEMA_VERSION} for executable tasks",
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
            "non-epic issue must satisfy the required headings. Criterion "
            "checks and Targeted tests are optional aliases. Notes may carry "
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
