"""Shared building blocks for narrow, hand-written Jev questions.

Every Ortus Jev question follows the same rules: one narrow question per
decision, about one item per request; every pick offers ``none`` and is paired
with an "is there any real basis" yes/no; facts code can compute travel in a
trusted block and Jev never counts, dates or estimates them; issue text and
tool input travel in a separate untrusted block; option order is shuffled so a
tie does not resolve to whatever was listed first. The helpers here are pure
and deterministic, so a replay can rebuild exactly what one decision saw.

Confidence bands are labels for the log and for offline analysis. Nothing in
this module, and nothing that reads :func:`soft_band`, changes an action.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any, Mapping

#: The option every choice carries: no listed option has a basis in the facts.
NONE = "none"

#: The yes/no every pick is paired with.
HAS_BASIS = "has_basis"

NONE_CRITERIA: dict[str, Any] = {
    "what": "No listed option has a real basis in the facts.",
    "not_for": "A case where the facts support one of the other options.",
    "examples": ["the facts say nothing that would favor any listed option"],
}

#: Asymmetric soft bands: below LOW leans no, at or above HIGH leans yes.
SOFT_LOW = 0.40
SOFT_HIGH = 0.70


def with_none(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options plus a ``none`` option, which an existing entry never loses to."""
    result = deepcopy(dict(options))
    result.setdefault(NONE, deepcopy(NONE_CRITERIA))
    return result


def shuffled_choice(options: Mapping[str, Any], seed: str) -> tuple[dict[str, Any], list[str]]:
    """The options in a deterministic order seeded by one decision's id.

    Each option is ranked by a hash of the seed and its name, so the same seed
    always presents the same order and a different decision usually presents
    another one. The presented order is returned for the decision log.
    """
    order = sorted(
        options,
        key=lambda name: hashlib.sha256(f"{seed}\x00{name}".encode()).hexdigest(),
    )
    return {name: deepcopy(options[name]) for name in order}, order


def noul_question(instructions: str, true: list[str], false: list[str]) -> dict[str, Any]:
    """One yes/no question with literal contrastive criteria."""
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": list(true), "false": list(false)},
    }


def basis_question(subject: str) -> dict[str, Any]:
    """Is there any real basis in the facts for ``subject``?"""
    return noul_question(
        f"Is there any real basis in the facts for {subject}?",
        ["A specific fact or a specific line of the untrusted text supports it."],
        ["Nothing in the facts or the text points either way; it would be a guess."],
    )


def soft_band(p: float, low: float = SOFT_LOW, high: float = SOFT_HIGH) -> str:
    """A logged label for a probability. It never changes an action."""
    if p < low:
        return "low"
    if p >= high:
        return "high"
    return "uncertain"


def soft_bands(narrow: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The soft band of every probability in a narrow answer set, for the log."""
    if narrow is None:
        return None
    return {
        name: soft_band(value) for name, value in narrow.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def split_state(facts: Mapping[str, Any], untrusted: Mapping[str, Any]) -> dict[str, Any]:
    """The System One payload: code-computed facts apart from untrusted text."""
    return {"facts": dict(facts), "untrusted": dict(untrusted)}


def has_basis(answers: Mapping[str, Any] | None, key: str = HAS_BASIS) -> bool:
    """Whether a narrow answer set claims a real basis for its pick.

    Missing answers have none, so a caller falls back onto its code baseline.
    """
    if not answers:
        return False
    value = answers.get(key)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0.5


# -- model router ------------------------------------------------------------

#: The router's own question set, asked in the pre_turn request (several narrow
#: questions about one bead in one request) and versioned apart from it.
ROUTER_VERSION = "router-v1"
CROSS_MODULE = "cross_module_reasoning"
NOVEL_ALGORITHM = "novel_algorithm"
ROUTER_HAS_BASIS = "router_has_basis"


def router_questions() -> dict[str, dict[str, Any]]:
    """Two narrow difficulty questions about the claimed bead, plus their basis."""
    return {
        CROSS_MODULE: noul_question(
            "Does this change need reasoning across more than one module?",
            ["The objective or design ties together behavior that lives in different modules."],
            ["The change stays inside one module and its tests."],
        ),
        NOVEL_ALGORITHM: noul_question(
            "Does this change need an algorithm or design not already present in the repository?",
            ["The spec asks for a new mechanism with no existing counterpart to follow."],
            ["The spec extends or reuses a pattern the repository already has."],
        ),
        ROUTER_HAS_BASIS: basis_question("the two difficulty answers about this change"),
    }


# -- spec facts ---------------------------------------------------------------

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_BACKTICKED = re.compile(r"`([^`\n]{1,200})`")
_CRITERION = re.compile(r"^\s*(?:[-*]\s*)?(AC-\d+)\b")
_FILE = re.compile(r"[\w.-]*/[\w./-]*|[\w-]+\.(?:py|md|toml|json|jsonl|yaml|yml|ts|tsx|js|go|rs|txt|jinja|sh)")
_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")
_RUNNERS = ("uv run", "pytest", "python -m", "npm ", "pnpm ", "yarn ", "go test",
            "cargo ", "make ", "bun ", "npx ")

#: Fixed spec sections whose sizes are reported, keyed by fact name. Only these
#: names reach the facts block: a heading a planner invented is issue text.
SPEC_SECTIONS: dict[str, tuple[str, str]] = {
    "objective": ("description", "Objective"),
    "behavioral_context": ("description", "Behavioral context"),
    "scope": ("design", "Scope"),
    "non_goals": ("design", "Non-goals"),
    "concrete_locations": ("design", "Concrete locations"),
    "resolved_decisions": ("design", "Resolved decisions"),
    "edge_cases": ("design", "Edge cases"),
    "observable_criteria": ("acceptance_criteria", "Observable criteria"),
    "criterion_checks": ("acceptance_criteria", "Criterion checks"),
}


def section(text: str, heading: str) -> str:
    """The body under one markdown heading, empty when the heading is absent."""
    lines: list[str] = []
    active = False
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            if active:
                break
            active = match.group(1).strip().casefold() == heading.casefold()
        elif active:
            lines.append(line)
    return "\n".join(lines).strip()


def _text(issue: Mapping[str, object], key: str) -> str:
    value = issue.get(key)
    return value if isinstance(value, str) else ""


def _concrete(design: str) -> tuple[int, int]:
    """Distinct files and symbols named in backticks under Concrete locations."""
    files: set[str] = set()
    symbols: set[str] = set()
    for span in _BACKTICKED.findall(section(design, "Concrete locations")):
        span = span.strip()
        if _FILE.fullmatch(span):
            files.add(span)
        elif _SYMBOL.fullmatch(span):
            symbols.add(span)
    return len(files), len(symbols)


def spec_facts(issue: Mapping[str, object], truncated: tuple[str, ...] = ()) -> dict[str, Any]:
    """Facts about a work spec that code computes, so Jev never has to count.

    Only numbers, fixed names and the packer's own field names: nothing here is
    issue text, which is what lets the block travel as trusted.
    """
    texts = {key: _text(issue, key) for key in ("description", "design", "acceptance_criteria")}
    acceptance = texts["acceptance_criteria"]
    criteria = section(acceptance, "Observable criteria") or acceptance
    checks = "\n".join(
        section(acceptance, heading) for heading in ("Criterion checks", "Targeted tests")
    )
    commands = {
        span.strip() for span in _BACKTICKED.findall(checks)
        if span.strip().startswith(_RUNNERS)
    }
    files, symbols = _concrete(texts["design"])
    estimate = issue.get("estimated_minutes")
    priority = issue.get("priority")
    return {
        "section_chars": {
            name: len(section(texts[field], heading))
            for name, (field, heading) in SPEC_SECTIONS.items()
        },
        "acceptance_criteria": len({
            match.group(1) for line in criteria.splitlines()
            if (match := _CRITERION.match(line))
        }),
        "concrete_files": files,
        "concrete_symbols": symbols,
        "test_commands": len(commands),
        "estimated_minutes": estimate if type(estimate) is int and estimate >= 0 else None,
        "priority": priority if type(priority) is int else None,
        "truncated_fields": sorted(truncated),
    }
