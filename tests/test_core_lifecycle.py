"""The declared state machines, and their agreement with the running code.

These tests are the drift alarm: a new journal phase, a renamed state, or a
classification set that stops matching the declaration fails here rather than
quietly making `README.md` wrong.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ortus.commands.grind import _FINALIZABLE_PHASES, _SEALED_PHASES
from ortus.core import lifecycle
from ortus.core.lifecycle import (
    CANDIDATE_MACHINE,
    CANDIDATE_PHASES,
    FINALIZATION_STEPS,
    ISSUE_MACHINE,
    LOG_LABELS,
    LifecycleError,
    StateMachine,
    Transition,
    build_candidate_machine,
    finalized_phase,
)
from ortus.core.runstate import PHASE_IDLE, TERMINAL_PHASES

SRC = Path(__file__).resolve().parents[1] / "src" / "ortus"

#: The only callee whose `phase=` argument is a log label rather than state.
LOG_LABEL_CALLEES = frozenset({"_enforce_branch_discipline"})


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


def _resolve(leaf: ast.expr) -> str | None:
    """The phase string a value expression names, when that is knowable."""

    if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
        # An empty default (`prior_phase: str = ""`) is the absence of a phase.
        return leaf.value or None
    if isinstance(leaf, ast.Name):
        resolved = getattr(lifecycle, leaf.id, None)
        return resolved if isinstance(resolved, str) else None
    return None


# ---------------------------------------------------------------------------
# AC-1 / AC-2: the declaration
# ---------------------------------------------------------------------------


def test_declares_both_machines() -> None:
    for machine in (ISSUE_MACHINE, CANDIDATE_MACHINE):
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
    assert CANDIDATE_MACHINE.initial == "implementation"
    # The two machines are deliberately separate: no state name is shared.
    assert not set(ISSUE_MACHINE.states) & set(CANDIDATE_MACHINE.states)


def test_finalized_states_derived_from_steps() -> None:
    declared = [s for s in CANDIDATE_MACHINE.states if s.startswith("finalized-")]
    assert declared == [finalized_phase(step) for step in FINALIZATION_STEPS]

    # A new boundary must appear in the graph without editing the declaration.
    # `attest` is hypothetical on purpose: a step this repository already
    # declares would prove the derivation only for a state someone had already
    # hand-checked into the graph.
    grown = build_candidate_machine((*FINALIZATION_STEPS[:-1], "attest", "sync"))
    assert finalized_phase("attest") in grown.states
    assert grown.terminal >= {finalized_phase("sync")}
    # ... wired into the chain, not stranded.
    grown.validate()
    assert finalized_phase("attest") in grown.reachable()
    assert any(
        t.source == finalized_phase("attest") and t.target == finalized_phase("sync")
        for t in grown.transitions
    )


# ---------------------------------------------------------------------------
# AC-5 / AC-9: code agrees with the declaration
# ---------------------------------------------------------------------------


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
            if not leaf.value:
                continue
            bare.append(f"{rel}:{line}: phase={leaf.value!r} (callee {callee})")

    assert not bare, (
        "journal phases must be assigned from an ortus.core.lifecycle constant:\n"
        + "\n".join(bare)
    )


def test_undeclared_phase_fails() -> None:
    seen: dict[str, str] = {}
    for rel, line, callee, value in _phase_writes():
        if callee in LOG_LABEL_CALLEES:
            continue
        for leaf in _leaves(value):
            resolved = _resolve(leaf)
            if resolved is not None:
                seen.setdefault(resolved, f"{rel}:{line}")

    assert seen, "no journal phase writes were resolved; the scanner is broken"
    undeclared = sorted(
        f"{phase!r} at {where}"
        for phase, where in seen.items()
        if phase not in CANDIDATE_PHASES
    )
    assert not undeclared, (
        "these journal phases are written by code but not declared in "
        "ortus.core.lifecycle:\n" + "\n".join(undeclared)
    )

    # And the declaration itself refuses a state it does not know.
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
# AC-6: the runtime classification sets
# ---------------------------------------------------------------------------


def test_classification_sets_are_declared() -> None:
    for name, members in (
        ("TERMINAL_PHASES", TERMINAL_PHASES),
        ("_SEALED_PHASES", _SEALED_PHASES),
        ("_FINALIZABLE_PHASES", _FINALIZABLE_PHASES),
    ):
        stray = sorted(members - CANDIDATE_PHASES)
        assert not stray, f"{name} contains undeclared phases: {stray}"

    # Membership is pinned: routing these through the declaration must not have
    # moved a single phase between them.
    assert TERMINAL_PHASES == {
        "corrections-exhausted",
        "correction-rejected",
        "plan-gap-escalated",
        "orphaned-candidate",
        "incomplete-candidate",
    }
    assert _SEALED_PHASES == {
        "implementation-timeout",
        "verification-timeout",
        "correction-timeout",
        "orphaned-candidate",
        "incomplete-candidate",
    }
    assert _FINALIZABLE_PHASES == {
        "verified-pass",
        "finalization-blocked",
        "finalized-report",
        "finalized-close",
        "finalized-compose",
        "finalized-commit",
    }


# ---------------------------------------------------------------------------
# AC-7 / AC-8: graph completeness, and what is not a state
# ---------------------------------------------------------------------------


def test_graph_is_reachable_and_has_no_dead_ends() -> None:
    for machine in (ISSUE_MACHINE, CANDIDATE_MACHINE):
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
    for label in LOG_LABELS:
        assert label not in CANDIDATE_PHASES
        assert label not in set(ISSUE_MACHINE.states)
    # The idle sentinel a snapshot reports without a journal is not one either.
    assert PHASE_IDLE not in CANDIDATE_PHASES

    # Every log label the code actually passes is one of the declared four.
    used = {
        leaf.value
        for _rel, _line, callee, value in _phase_writes()
        if callee in LOG_LABEL_CALLEES
        for leaf in _leaves(value)
        if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)
    }
    assert used and used <= set(LOG_LABELS)
