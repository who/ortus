"""The declared issue machine, and its agreement with the running code.

These tests are the drift alarm: a renamed status, or a log-label that stops
matching the declaration, fails here rather than quietly making `README.md`
wrong.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ortus.core import lifecycle
from ortus.core.lifecycle import (
    ISSUE_MACHINE,
    LOG_LABELS,
    LifecycleError,
    StateMachine,
    Transition,
)
from ortus.core.prompts import PROMPT_REGISTRY
from ortus.core.runstate import PHASE_IDLE, TERMINAL_PHASES

SRC = Path(__file__).resolve().parents[1] / "src" / "ortus"

#: The only callee whose `phase=` argument is a log label rather than state.
LOG_LABEL_CALLEES = frozenset({"_enforce_branch_discipline"})

#: Callees whose `phase=` names a workflow stage for `ortus prompt list`,
#: not journal state. Their literals must match the registry's declarations.
PROMPT_REGISTRY_CALLEES = frozenset({"PromptInfo"})
PROMPT_PHASES = frozenset(entry.phase for entry in PROMPT_REGISTRY)


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def _callee_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return "<unknown>"


def _leaves(value: ast.expr) -> list[ast.expr]:
    """Flatten a conditional expression into the values it can produce."""

    if isinstance(value, ast.IfExp):
        return _leaves(value.body) + _leaves(value.orelse)
    return [value]


def _phase_writes() -> list[tuple[str, int, str, ast.expr]]:
    """Every place a phase value is written, as (path, line, callee, value).

    Two syntactic shapes carry a phase: a `phase=` keyword argument, and an
    assignment to a name ending in `phase` (grind builds `timeout_phase` and
    `halt_phase` before handing them on). Classification is by callee, per the
    inventory in the `lifecycle` module docstring.
    """

    found: list[tuple[str, int, str, ast.expr]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "phase":
                        found.append(
                            (rel, node.lineno, _callee_name(node), keyword.value)
                        )
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                if node.value is None:
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    name = getattr(target, "id", None) or getattr(target, "attr", "")
                    if name.lower().endswith("phase"):
                        found.append((rel, node.lineno, "<assignment>", node.value))
    return found


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_candidate_machine_is_gone() -> None:
    for name in (
        "CANDIDATE_MACHINE",
        "CANDIDATE_PHASES",
        "COUPLINGS",
        "FINALIZATION_STEPS",
        "FINALIZED_PREFIX",
        "build_candidate_machine",
        "finalized_phase",
        "mermaid_candidate_graph",
    ):
        assert not hasattr(lifecycle, name), name


def test_declares_issue_machine() -> None:
    machine = ISSUE_MACHINE
    assert machine.initial in machine.states
    assert machine.terminal
    assert machine.terminal <= set(machine.states)
    assert machine.transitions
    for transition in machine.transitions:
        assert transition.source in machine.states
        assert transition.target in machine.states
        assert transition.trigger.strip()
    machine.validate()

    assert ISSUE_MACHINE.initial == "open"
    assert set(ISSUE_MACHINE.states) == {"open", "in_progress", "closed"}


def test_no_bare_phase_literals() -> None:
    writes = _phase_writes()
    assert writes, "the phase scanner matched nothing; it has stopped working"

    bare: list[str] = []
    for rel, line, callee, value in writes:
        for leaf in _leaves(value):
            if not (isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)):
                continue
            if callee in LOG_LABEL_CALLEES:
                assert leaf.value in LOG_LABELS, (
                    f"{rel}:{line}: undeclared log label {leaf.value!r}"
                )
                continue
            if callee in PROMPT_REGISTRY_CALLEES:
                assert leaf.value in PROMPT_PHASES, (
                    f"{rel}:{line}: undeclared prompt phase {leaf.value!r}"
                )
                continue
            if not leaf.value:
                continue
            bare.append(f"{rel}:{line}: phase={leaf.value!r} (callee {callee})")

    assert not bare, (
        "journal phases must not be written; live grind does not persist them:\n"
        + "\n".join(bare)
    )


def test_undeclared_transition_target_fails() -> None:
    with pytest.raises(LifecycleError, match="not a declared state"):
        StateMachine(
            name="broken",
            title="broken",
            summary="",
            initial="a",
            states=("a", "b"),
            terminal=frozenset({"b"}),
            transitions=(Transition("a", "somewhere-undeclared", "t"),),
        ).validate()


# ---------------------------------------------------------------------------
# Runtime leftover-log heuristics stay local to runstate
# ---------------------------------------------------------------------------


def test_terminal_phases_are_local_historical_names() -> None:
    assert TERMINAL_PHASES == {
        "corrections-exhausted",
        "correction-rejected",
        "plan-gap-escalated",
        "orphaned-candidate",
        "incomplete-candidate",
    }
    assert not hasattr(lifecycle, "CORRECTIONS_EXHAUSTED")
    assert not hasattr(lifecycle, "FINALIZED_PREFIX")


# ---------------------------------------------------------------------------
# Graph completeness, and what is not a state
# ---------------------------------------------------------------------------


def test_graph_is_reachable_and_has_no_dead_ends() -> None:
    machine = ISSUE_MACHINE
    assert machine.unreachable() == (), (
        f"{machine.name}: states unreachable from {machine.initial!r}: "
        f"{machine.unreachable()}"
    )
    assert machine.dead_ends() == (), (
        f"{machine.name}: non-terminal states with no way out: "
        f"{machine.dead_ends()}"
    )
    for state in machine.terminal:
        assert machine.outgoing(state) == ()


def test_log_labels_are_not_states() -> None:
    assert LOG_LABELS == ("startup", "pre-iter", "post-close", "post-housekeeping")
    issue_states = set(ISSUE_MACHINE.states)
    for label in LOG_LABELS:
        assert label not in issue_states
    assert PHASE_IDLE not in issue_states

    used = {
        leaf.value
        for _rel, _line, callee, value in _phase_writes()
        if callee in LOG_LABEL_CALLEES
        for leaf in _leaves(value)
        if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)
    }
    assert used and used <= set(LOG_LABELS)
