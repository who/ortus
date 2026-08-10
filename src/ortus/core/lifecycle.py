"""The two state machines Ortus runs, declared once as data.

Ortus drives two lifecycles that are easy to confuse because both are spelled
as short lowercase strings:

* the **bd issue status** machine — ``open`` / ``in_progress`` / ``closed``,
  owned by bd and read and written by :mod:`ortus.core.bd`; it outlives any
  single grind run; and
* the **candidate journal phase** machine — the ``phase`` field of
  :class:`ortus.core.transaction.CandidateJournal`, which exists only for the
  duration of one candidate transaction.

Both are declared here, together with the points at which they interact, and
both are rendered into ``README.md`` between the ``state-graph`` generated
markers by :func:`render_readme_block`. ``tests/test_state_graph_docs.py``
fails when the committed README block and the renderer disagree, so a state
cannot change without the documentation changing with it.

Journal phases versus log labels
--------------------------------

``phase=`` appears as a keyword argument at two unrelated kinds of call site,
and only one of them writes journal state. Classified by callee:

*Journal phases* (persisted into ``CandidateJournal.phase``) come from
``CandidateJournal.with_candidate``, ``finish_verification``,
``begin_verification``, ``begin_correction``, ``route_plan_gap``,
``with_finalization``, ``dataclasses.replace(journal, phase=...)`` and grind's
``_capture_codex_candidate`` / ``_reject`` wrappers around them.

*Log labels* are the ``phase=`` argument of
``grind._enforce_branch_discipline``. They tag a line in the run log with when
branch discipline ran and are never persisted as state: ``startup``,
``pre-iter``, ``post-close`` and ``post-housekeeping`` (see :data:`LOG_LABELS`).
``runstate.PHASE_IDLE`` (``"idle"``) is a third non-state: it is what a
snapshot reports when no journal exists at all.

Classification sets
-------------------

Three frozensets classify journal phases at runtime and are validated against
this declaration rather than duplicating it:
``runstate.TERMINAL_PHASES``, ``grind._SEALED_PHASES`` and
``grind._FINALIZABLE_PHASES``.

One asymmetry between them is deliberate and pre-existing: ``runstate`` treats
every ``finalized-*`` phase as terminal for dashboard display, while grind
treats all but the last of them as resumable (they are in
``_FINALIZABLE_PHASES``). The dashboard is reporting "this transaction reached
a finalization boundary"; grind is deciding "this transaction still owes work".
:data:`CANDIDATE_MACHINE` models grind's view, because that is the one that
governs transitions.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BEGIN_MARKER",
    "CANDIDATE_MACHINE",
    "COUPLINGS",
    "END_MARKER",
    "FINALIZATION_STEPS",
    "FINALIZED_PREFIX",
    "ISSUE_MACHINE",
    "LOG_LABELS",
    "Coupling",
    "LifecycleError",
    "StateMachine",
    "Transition",
    "build_candidate_machine",
    "finalized_phase",
    "mermaid_candidate_graph",
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


# ---------------------------------------------------------------------------
# Candidate journal phases
# ---------------------------------------------------------------------------

#: The phase a journal starts in: a worker is editing, nothing is sealed yet.
IMPLEMENTATION = "implementation"
#: A journal that could not be loaded, rebuilt from the lone claim and the
#: dirty worktree so the inherited work keeps an owner.
HANDOFF = "handoff"
#: The worker returned and grind sealed its diff for review.
CANDIDATE_CAPTURED = "candidate-captured"
#: Resumable: the implementation worker ran out of wall clock.
IMPLEMENTATION_TIMEOUT = "implementation-timeout"
#: The implementation isolation guard refused the candidate.
IMPLEMENTATION_REJECTED = "implementation-rejected"
#: A fresh read-only verifier is running against the sealed candidate.
VERIFICATION = "verification"
#: Resumable: the verifier ran out of wall clock without touching the candidate.
VERIFICATION_TIMEOUT = "verification-timeout"
#: The verifier produced no usable verdict, or moved the candidate.
VERIFICATION_REJECTED = "verification-rejected"
#: The verifier passed the candidate; finalization may begin.
VERIFIED_PASS = "verified-pass"
#: The verifier failed the candidate; a correction may follow.
VERIFIED_FAIL = "verified-fail"
#: A bounded correction attempt is running.
CORRECTION = "correction"
#: Resumable: the correction worker ran out of wall clock.
CORRECTION_TIMEOUT = "correction-timeout"
#: A rejection arrived after a correction had already been spent.
CORRECTION_REJECTED = "correction-rejected"
#: The bounded correction budget is gone.
CORRECTIONS_EXHAUSTED = "corrections-exhausted"
#: One planning-profile pass was spent on a material planning gap.
PLAN_GAP_ROUTED = "plan-gap-routed"
#: The planning gap survived its one pass; a human owns it now.
PLAN_GAP_ESCALATED = "plan-gap-escalated"
#: A claim outlived its worker with edits in the tree.
ORPHANED_CANDIDATE = "orphaned-candidate"
#: The worker returned with its claim still open and no verdict.
INCOMPLETE_CANDIDATE = "incomplete-candidate"
#: Legacy condition-mode path: the Codex worker closed the issue itself and
#: grind is committing the owned paths behind it.
FINALIZING = "finalizing"
#: A finalization precondition failed; the journal is kept for the replay.
FINALIZATION_BLOCKED = "finalization-blocked"

#: Ordered finalization boundaries. Each one is journaled *after* it lands, so
#: a restart replays only the steps that never completed and can never
#: duplicate a comment, close, commit, or push. Declared here rather than in
#: :mod:`ortus.core.transaction` (which re-exports it) so the phase graph can
#: derive its ``finalized-*`` states without importing the journal.
FINALIZATION_STEPS: tuple[str, ...] = (
    "report",
    "close",
    "compose",
    "commit",
    "sync",
)
FINALIZED_PREFIX = "finalized-"

#: `phase=` values that tag a log line instead of naming a state. They are
#: arguments to ``grind._enforce_branch_discipline`` and never reach a journal.
LOG_LABELS: tuple[str, ...] = (
    "startup",
    "pre-iter",
    "post-close",
    "post-housekeeping",
)


def finalized_phase(step: str) -> str:
    """The journal phase written once finalization boundary `step` lands."""

    return f"{FINALIZED_PREFIX}{step}"


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
    #: diagram draws only these, because a reader's first question is how a
    #: candidate reaches a commit, and a graph carrying every timeout, refusal
    #: and halt alongside it answers that question worse than no graph at all.
    #: Nothing is hidden: the table beneath the diagram carries every
    #: transition, and rows are exhaustive where pictures stop scaling.
    main_path: tuple[str, ...] = ()

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


@dataclass(frozen=True)
class Coupling:
    """One point where the two machines touch."""

    candidate_phase: str
    issue_transition: str
    description: str


# ---------------------------------------------------------------------------
# The bd issue status machine
# ---------------------------------------------------------------------------

ISSUE_MACHINE = StateMachine(
    name="issue",
    title="bd issue status",
    summary=(
        "The statuses Ortus reads and writes through `bd`. One issue moves "
        "through this machine across however many grind runs it takes."
    ),
    initial=ISSUE_OPEN,
    states=(ISSUE_OPEN, ISSUE_IN_PROGRESS, ISSUE_CLOSED),
    terminal=frozenset({ISSUE_CLOSED}),
    main_path=(ISSUE_OPEN, ISSUE_IN_PROGRESS, ISSUE_CLOSED),
    transitions=(
        Transition(ISSUE_OPEN, ISSUE_IN_PROGRESS, "grind claims the selected issue"),
        Transition(
            ISSUE_IN_PROGRESS,
            ISSUE_IN_PROGRESS,
            "grind restores a claim a worker released without authority",
        ),
        Transition(
            ISSUE_IN_PROGRESS,
            ISSUE_OPEN,
            "orphan policy revert releases a claim that outlived its worker",
        ),
        Transition(
            ISSUE_IN_PROGRESS,
            ISSUE_CLOSED,
            "finalization closes the verified issue",
        ),
    ),
)


# ---------------------------------------------------------------------------
# The candidate journal phase machine
# ---------------------------------------------------------------------------


def build_candidate_machine(
    finalization_steps: tuple[str, ...] = FINALIZATION_STEPS,
) -> StateMachine:
    """Declare the candidate phase machine for a given finalization sequence.

    The ``finalized-*`` states and the chain between them are derived from
    `finalization_steps`, so adding a boundary to :data:`FINALIZATION_STEPS`
    adds a state and its edges without anyone editing this declaration.
    """

    if not finalization_steps:
        raise LifecycleError("candidate: at least one finalization step is required")
    finalized = tuple(finalized_phase(step) for step in finalization_steps)
    last_finalized = finalized[-1]

    states: tuple[str, ...] = (
        IMPLEMENTATION,
        HANDOFF,
        CANDIDATE_CAPTURED,
        IMPLEMENTATION_TIMEOUT,
        IMPLEMENTATION_REJECTED,
        VERIFICATION,
        VERIFICATION_TIMEOUT,
        VERIFICATION_REJECTED,
        VERIFIED_PASS,
        VERIFIED_FAIL,
        CORRECTION,
        CORRECTION_TIMEOUT,
        CORRECTION_REJECTED,
        CORRECTIONS_EXHAUSTED,
        PLAN_GAP_ROUTED,
        PLAN_GAP_ESCALATED,
        ORPHANED_CANDIDATE,
        INCOMPLETE_CANDIDATE,
        FINALIZING,
        FINALIZATION_BLOCKED,
        *finalized,
    )

    transitions: list[Transition] = [
        Transition(
            IMPLEMENTATION,
            HANDOFF,
            "an unusable journal is rebuilt from the lone claim and the dirty tree",
        ),
        Transition(
            IMPLEMENTATION,
            CANDIDATE_CAPTURED,
            "the worker returned and grind sealed its diff",
        ),
        Transition(
            IMPLEMENTATION,
            IMPLEMENTATION_TIMEOUT,
            "the worker ran out of wall clock",
        ),
        Transition(
            HANDOFF,
            CANDIDATE_CAPTURED,
            "the resumed worker returned and grind sealed its diff",
        ),
        Transition(
            HANDOFF,
            IMPLEMENTATION_TIMEOUT,
            "the resumed worker ran out of wall clock",
        ),
        Transition(
            IMPLEMENTATION_TIMEOUT,
            CANDIDATE_CAPTURED,
            "a restart resumes the same issue and a fresh worker finishes it",
        ),
        Transition(
            CANDIDATE_CAPTURED,
            IMPLEMENTATION_REJECTED,
            "the implementation isolation guard refused the candidate",
        ),
        Transition(
            CANDIDATE_CAPTURED,
            ORPHANED_CANDIDATE,
            "the claim outlived its worker",
        ),
        Transition(
            CANDIDATE_CAPTURED,
            INCOMPLETE_CANDIDATE,
            "the worker returned with its claim still open",
        ),
        Transition(
            CANDIDATE_CAPTURED,
            FINALIZING,
            "legacy condition mode; the Codex worker closed the issue itself",
        ),
        Transition(
            CANDIDATE_CAPTURED,
            VERIFICATION,
            "a fresh read-only verifier starts",
        ),
        Transition(
            IMPLEMENTATION_REJECTED,
            CANDIDATE_CAPTURED,
            "a restart re-implements the rejected candidate",
        ),
        Transition(
            VERIFICATION, VERIFIED_PASS, "the verifier returned a passing verdict"
        ),
        Transition(
            VERIFICATION, VERIFIED_FAIL, "the verifier returned a failing verdict"
        ),
        Transition(
            VERIFICATION,
            VERIFICATION_REJECTED,
            "the verifier produced no usable verdict, or moved the candidate",
        ),
        Transition(
            VERIFICATION,
            VERIFICATION_TIMEOUT,
            "the verifier ran out of wall clock with the candidate intact",
        ),
        Transition(
            VERIFICATION_TIMEOUT,
            VERIFICATION,
            "a restart re-verifies the preserved candidate",
        ),
        Transition(
            VERIFICATION_TIMEOUT,
            CORRECTION_REJECTED,
            "a correction had already been spent on this candidate",
        ),
        Transition(
            VERIFICATION_REJECTED,
            CANDIDATE_CAPTURED,
            "a restart re-implements after a rejected verification",
        ),
        Transition(
            VERIFICATION_REJECTED,
            CORRECTION_REJECTED,
            "a correction had already been spent on this candidate",
        ),
        Transition(
            VERIFIED_FAIL, CORRECTION, "a correction attempt remains in the budget"
        ),
        Transition(
            VERIFIED_FAIL,
            CORRECTIONS_EXHAUSTED,
            "the bounded correction budget is spent",
        ),
        Transition(
            VERIFIED_FAIL,
            PLAN_GAP_ROUTED,
            "the findings name a planning gap; one planning pass is spent",
        ),
        Transition(
            VERIFIED_FAIL,
            PLAN_GAP_ESCALATED,
            "the planning gap survived its one planning pass",
        ),
        Transition(
            CORRECTION,
            CANDIDATE_CAPTURED,
            "the correction worker returned and grind re-sealed the diff",
        ),
        Transition(
            CORRECTION,
            CORRECTION_TIMEOUT,
            "the correction worker ran out of wall clock",
        ),
        Transition(
            CORRECTION_TIMEOUT,
            CANDIDATE_CAPTURED,
            "a restart re-implements the timed-out correction",
        ),
        Transition(
            PLAN_GAP_ROUTED,
            CANDIDATE_CAPTURED,
            "a restart re-implements against the replanned issue",
        ),
        Transition(
            VERIFIED_PASS,
            finalized[0],
            f"finalization boundary {finalization_steps[0]} landed",
        ),
        Transition(
            VERIFIED_PASS,
            FINALIZATION_BLOCKED,
            "a finalization precondition failed",
        ),
    ]
    for previous, step, state in zip(finalized, finalization_steps[1:], finalized[1:]):
        transitions.append(
            Transition(previous, state, f"finalization boundary {step} landed")
        )
    for state in finalized[:-1]:
        transitions.append(
            Transition(
                state,
                FINALIZATION_BLOCKED,
                "a finalization precondition failed on replay",
            )
        )
    for state in finalized:
        transitions.append(
            Transition(
                FINALIZATION_BLOCKED,
                state,
                "a restart replays the first boundary that has not landed",
            )
        )

    machine = StateMachine(
        name="candidate",
        title="Candidate journal phase",
        summary=(
            "`CandidateJournal.phase` for one candidate transaction, from the "
            "first worker edit to a committed candidate or a halt a human owns."
        ),
        initial=IMPLEMENTATION,
        states=states,
        terminal=frozenset(
            {
                CORRECTION_REJECTED,
                CORRECTIONS_EXHAUSTED,
                PLAN_GAP_ESCALATED,
                ORPHANED_CANDIDATE,
                INCOMPLETE_CANDIDATE,
                FINALIZING,
                last_finalized,
            }
        ),
        transitions=tuple(transitions),
        # Worker edit to committed candidate, with nothing going wrong. The
        # finalized-* steps are listed individually because their order is the
        # crash-resume contract, and a reader who cannot see it in the diagram
        # has to reconstruct it from the table.
        main_path=(
            IMPLEMENTATION,
            CANDIDATE_CAPTURED,
            VERIFICATION,
            VERIFIED_PASS,
            *finalized,
        ),
    )
    machine.validate()
    return machine


CANDIDATE_MACHINE = build_candidate_machine()
ISSUE_MACHINE.validate()

#: Every declared candidate phase. The one set a journal phase must belong to.
CANDIDATE_PHASES: frozenset[str] = frozenset(CANDIDATE_MACHINE.states)


# ---------------------------------------------------------------------------
# Where the two machines meet
# ---------------------------------------------------------------------------

COUPLINGS: tuple[Coupling, ...] = (
    Coupling(
        candidate_phase=finalized_phase("close"),
        issue_transition=f"{ISSUE_IN_PROGRESS} -> {ISSUE_CLOSED}",
        description=(
            "Only finalization closes an issue, and only after a fresh verifier "
            "passed the candidate."
        ),
    ),
    Coupling(
        candidate_phase=FINALIZING,
        issue_transition=f"already {ISSUE_CLOSED}",
        description=(
            "Legacy condition mode: the Codex worker closed the issue itself, so "
            "grind only commits the owned paths behind it."
        ),
    ),
    Coupling(
        candidate_phase=f"{ORPHANED_CANDIDATE}, {IMPLEMENTATION_TIMEOUT}",
        issue_transition=f"{ISSUE_IN_PROGRESS} -> {ISSUE_OPEN}",
        description=(
            "Orphan policy `revert` releases the claim; the candidate stays in "
            "the worktree and the journal keeps the issue association, so the "
            "next run re-claims the same issue."
        ),
    ),
    Coupling(
        candidate_phase=f"{PLAN_GAP_ESCALATED}, {CORRECTIONS_EXHAUSTED}, "
        f"{CORRECTION_REJECTED}",
        issue_transition=f"stays {ISSUE_IN_PROGRESS}, labelled `human`",
        description=(
            "A halt a human owns: no close, no commit. The issue keeps its claim "
            "so nothing else selects it."
        ),
    ),
    Coupling(
        candidate_phase=f"{VERIFICATION}, {CORRECTION}",
        issue_transition=f"{ISSUE_IN_PROGRESS} -> {ISSUE_IN_PROGRESS}",
        description=(
            "A worker that changed the status despite the phase contract cannot "
            "make that stick; grind restores the claim before continuing."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

BEGIN_MARKER = "<!-- BEGIN GENERATED: state-graph -->"
END_MARKER = "<!-- END GENERATED: state-graph -->"


def _node_id(state: str) -> str:
    """A Mermaid-safe node id; hyphens are not legal in a bare state id."""

    return state.replace("-", "_")


def render_mermaid(machine: StateMachine) -> str:
    """Render `machine`'s main path as deterministic ``stateDiagram-v2`` text.

    Only main-path states and the transitions between them are drawn. The
    declaration holds 29 states and 57 transitions across the two machines,
    and a diagram carrying all of them routes edges around each other until
    the main line is impossible to trace — the picture stops being a picture.
    Every transition still appears in :func:`render_transition_table`.

    Ordering follows the declaration, and nothing here reads the clock or the
    environment, so the output is stable across runs.
    """

    drawn = machine.main_path or machine.states
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
        if transition.source == transition.target:
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


def mermaid_candidate_graph() -> str:
    """The candidate journal phase machine as a Mermaid state diagram."""

    return render_mermaid(CANDIDATE_MACHINE)


def _machine_section(machine: StateMachine, graph: str) -> list[str]:
    drawn = len(machine.main_path or machine.states)
    total = len(machine.states)
    note = (
        f"The diagram is the path through when nothing goes wrong — {drawn} of "
        f"{total} states. Timeouts, refusals, plan gaps and halts are real and "
        "are listed in full beneath it."
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
    """The exact text README carries between the state-graph markers."""

    lines = [
        "<!-- Generated from src/ortus/core/lifecycle.py. Do not edit by hand: "
        "tests/test_state_graph_docs.py fails and prints the correct block. -->",
        "",
    ]
    lines += _machine_section(ISSUE_MACHINE, mermaid_issue_graph())
    lines += _machine_section(CANDIDATE_MACHINE, mermaid_candidate_graph())
    lines += [
        "#### Where the two machines meet",
        "",
        "| Candidate phase | Issue status | What it means |",
        "| --- | --- | --- |",
    ]
    for coupling in COUPLINGS:
        lines.append(
            f"| `{coupling.candidate_phase}` | {coupling.issue_transition} | "
            f"{coupling.description} |"
        )
    lines += [
        "",
        "`"
        + "`, `".join(LOG_LABELS)
        + "` are *not* journal phases. They are the `phase=` argument of "
        "grind's branch-discipline logging, and they never reach a journal; "
        "neither does `idle`, which a run snapshot reports when no journal "
        "exists at all.",
    ]
    return "\n".join(lines)


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
