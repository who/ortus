"""The bd issue status machine Ortus reads and writes, declared once as data.

The live close path is the issue machine only: ``open`` / ``in_progress`` /
``closed``, owned by bd and read and written by :mod:`ortus.core.bd`. It
outlives any single grind run.

The declaration is rendered into ``README.md`` between the ``state-graph``
generated markers by :func:`render_readme_block`.
``tests/test_state_graph_docs.py`` fails when the committed README block and
the renderer disagree, so a status cannot change without the documentation
changing with it.

Log labels
----------

``phase=`` also appears as the argument of
``grind._enforce_branch_discipline``. Those values tag a line in the run log
with when branch discipline ran and are never persisted as state:
``startup``, ``pre-iter``, ``post-close`` and ``post-housekeeping`` (see
:data:`LOG_LABELS`). ``runstate.PHASE_IDLE`` (``"idle"``) is a third
non-state: it is what a snapshot reports when no grind log exists.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "ISSUE_MACHINE",
    "LOG_LABELS",
    "LifecycleError",
    "StateMachine",
    "Transition",
    "mermaid_issue_graph",
    "readme_block",
    "render_mermaid",
    "render_readme_block",
]


class LifecycleError(ValueError):
    """Raised when a declaration or a generated document is inconsistent."""


# ---------------------------------------------------------------------------
# bd issue statuses Ortus reads and writes
# ---------------------------------------------------------------------------

ISSUE_OPEN = "open"
ISSUE_IN_PROGRESS = "in_progress"
ISSUE_CLOSED = "closed"

#: `phase=` values that tag a log line instead of naming a state. They are
#: arguments to ``grind._enforce_branch_discipline`` and never reach a journal.
LOG_LABELS: tuple[str, ...] = (
    "startup",
    "pre-iter",
    "post-close",
    "post-housekeeping",
)


# ---------------------------------------------------------------------------
# Declaration primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """One legal move, and the trigger that causes it."""

    source: str
    target: str
    trigger: str


@dataclass(frozen=True)
class StateMachine:
    """An immutable state machine declaration."""

    name: str
    title: str
    summary: str
    initial: str
    states: tuple[str, ...]
    terminal: frozenset[str]
    transitions: tuple[Transition, ...]
    #: The states a run passes through when nothing goes wrong, in order. The
    #: diagram draws only these, because a reader's first question is how an
    #: issue reaches closed, and a graph carrying every halt alongside it
    #: answers that question worse than no graph at all. Nothing is hidden:
    #: the table beneath the diagram carries every transition, and rows are
    #: exhaustive where pictures stop scaling.
    happy_path: tuple[str, ...] = ()

    def outgoing(self, state: str) -> tuple[Transition, ...]:
        """Transitions that leave `state`, excluding self-loops."""

        return tuple(
            t for t in self.transitions if t.source == state and t.target != state
        )

    def reachable(self) -> frozenset[str]:
        """Every state reachable from :attr:`initial`."""

        seen = {self.initial}
        frontier = [self.initial]
        while frontier:
            current = frontier.pop()
            for transition in self.transitions:
                if transition.source == current and transition.target not in seen:
                    seen.add(transition.target)
                    frontier.append(transition.target)
        return frozenset(seen)

    def unreachable(self) -> tuple[str, ...]:
        reachable = self.reachable()
        return tuple(state for state in self.states if state not in reachable)

    def dead_ends(self) -> tuple[str, ...]:
        """Non-terminal states that cannot be left."""

        return tuple(
            state
            for state in self.states
            if state not in self.terminal and not self.outgoing(state)
        )

    def validate(self) -> None:
        """Raise :class:`LifecycleError` on an incomplete declaration."""

        declared = set(self.states)
        if len(declared) != len(self.states):
            raise LifecycleError(f"{self.name}: duplicate state declaration")
        if self.initial not in declared:
            raise LifecycleError(
                f"{self.name}: initial state {self.initial!r} is not declared"
            )
        undeclared_terminal = sorted(self.terminal - declared)
        if undeclared_terminal:
            raise LifecycleError(
                f"{self.name}: terminal states are not declared: "
                + ", ".join(undeclared_terminal)
            )
        for transition in self.transitions:
            for endpoint in (transition.source, transition.target):
                if endpoint not in declared:
                    raise LifecycleError(
                        f"{self.name}: transition endpoint {endpoint!r} is not "
                        "a declared state"
                    )
            if transition.source in self.terminal:
                raise LifecycleError(
                    f"{self.name}: terminal state {transition.source!r} has an "
                    "outgoing transition"
                )
        unreachable = self.unreachable()
        if unreachable:
            raise LifecycleError(
                f"{self.name}: unreachable states: " + ", ".join(unreachable)
            )
        dead_ends = self.dead_ends()
        if dead_ends:
            raise LifecycleError(
                f"{self.name}: non-terminal states with no outgoing transition: "
                + ", ".join(dead_ends)
            )


# ---------------------------------------------------------------------------
# The bd issue status machine
# ---------------------------------------------------------------------------

ISSUE_MACHINE = StateMachine(
    name="issue",
    title="bd issue status",
    summary=(
        "The statuses Ortus reads and writes through `bd`. A worker claims an "
        "open issue, session-closes it, or leaves the claim in_progress for "
        "the next window or a human. Leftover in_progress is not reverted "
        "to open."
    ),
    initial=ISSUE_OPEN,
    states=(ISSUE_OPEN, ISSUE_IN_PROGRESS, ISSUE_CLOSED),
    terminal=frozenset({ISSUE_CLOSED}),
    happy_path=(ISSUE_OPEN, ISSUE_IN_PROGRESS, ISSUE_CLOSED),
    transitions=(
        Transition(ISSUE_OPEN, ISSUE_IN_PROGRESS, "the worker claims the selected issue"),
        Transition(
            ISSUE_IN_PROGRESS,
            ISSUE_IN_PROGRESS,
            "the leftover claim continues in the next window",
        ),
        Transition(
            ISSUE_IN_PROGRESS,
            ISSUE_IN_PROGRESS,
            "grind labels human and stops",
        ),
        Transition(
            ISSUE_IN_PROGRESS,
            ISSUE_CLOSED,
            "the worker session-closes the issue",
        ),
    ),
)
ISSUE_MACHINE.validate()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

BEGIN_MARKER = "<!-- BEGIN GENERATED: state-graph -->"
END_MARKER = "<!-- END GENERATED: state-graph -->"


def _node_id(state: str) -> str:
    """A Mermaid-safe node id; hyphens are not legal in a bare state id."""

    return state.replace("-", "_")


def render_mermaid(machine: StateMachine) -> str:
    """Render `machine`'s happy path as deterministic ``stateDiagram-v2`` text.

    Only happy-path states and the transitions between them are drawn. The
    issue machine is small enough that the happy path is the whole machine;
    the helper still exists so a later declaration can omit rare edges from
    the picture without losing them from :func:`render_transition_table`.

    Ordering follows the declaration, and nothing here reads the clock or the
    environment, so the output is stable across runs.
    """

    drawn = machine.happy_path or machine.states
    included = frozenset(drawn)
    lines = ["stateDiagram-v2", "    direction TB"]
    for state in drawn:
        node = _node_id(state)
        if node != state:
            lines.append(f'    state "{state}" as {node}')
    if machine.initial in included:
        lines.append(f"    [*] --> {_node_id(machine.initial)}")
    for transition in machine.transitions:
        if transition.source not in included or transition.target not in included:
            continue
        lines.append(
            f"    {_node_id(transition.source)} --> "
            f"{_node_id(transition.target)}: {transition.trigger}"
        )
    for state in drawn:
        if state in machine.terminal:
            lines.append(f"    {_node_id(state)} --> [*]")
    return "\n".join(lines)


def render_transition_table(machine: StateMachine) -> str:
    """Every transition `machine` declares, as a Markdown table.

    This is the exhaustive record the diagram deliberately is not. A table
    does not degrade as the machine grows, it greps, and it diffs a row at a
    time in review — which is what a reader chasing one specific failure path
    actually needs.
    """

    rows = [
        "| From | Trigger | To |",
        "| --- | --- | --- |",
    ]
    for transition in machine.transitions:
        rows.append(
            f"| `{transition.source}` | {transition.trigger} | "
            f"`{transition.target}` |"
        )
    return "\n".join(rows)


def mermaid_issue_graph() -> str:
    """The bd issue status machine as a Mermaid state diagram."""

    return render_mermaid(ISSUE_MACHINE)


def _machine_section(machine: StateMachine, graph: str) -> list[str]:
    drawn = len(machine.happy_path or machine.states)
    total = len(machine.states)
    note = (
        f"The diagram is the path through when nothing goes wrong — {drawn} of "
        f"{total} states. Timeouts, refusals, planning gaps and halts are real "
        "and are listed in full beneath it."
        if drawn < total
        else ""
    )
    lines = [
        f"#### {machine.title}",
        "",
        machine.summary,
        "",
    ]
    if note:
        lines += [note, ""]
    lines += [
        "```mermaid",
        graph,
        "```",
        "",
        f"<details><summary>Every {machine.name} transition "
        f"({len(machine.transitions)})</summary>",
        "",
        render_transition_table(machine),
        "",
        "</details>",
        "",
    ]
    return lines


def render_readme_block() -> str:
    """The exact text README carries between the state-graph markers.

    Operator-facing README shows the bd issue-status machine. The advertised
    close path is the worker session-closing the issue.
    """

    lines = [
        "<!-- Generated from src/ortus/core/lifecycle.py. Do not edit by hand: "
        "tests/test_state_graph_docs.py fails and prints the correct block. -->",
        "",
    ]
    lines += _machine_section(ISSUE_MACHINE, mermaid_issue_graph())
    return "\n".join(lines).rstrip()


def readme_block(text: str) -> str:
    """Extract the generated block from README `text`.

    Raises :class:`LifecycleError` with an actionable message when the markers
    are missing or duplicated, rather than returning a confusing diff.
    """

    for marker, label in ((BEGIN_MARKER, "begin"), (END_MARKER, "end")):
        count = text.count(marker)
        if count == 0:
            raise LifecycleError(
                f"README is missing the state-graph {label} marker: {marker}"
            )
        if count > 1:
            raise LifecycleError(
                f"README has {count} state-graph {label} markers; expected exactly one"
            )
    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        raise LifecycleError(
            "README state-graph markers are out of order: "
            f"{END_MARKER} appears before {BEGIN_MARKER}"
        )
    return text[start:end].strip("\n")
