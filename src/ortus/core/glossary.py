"""Ortus's vocabulary, declared once as data.

Ortus runs on a private vocabulary. ``packet``, ``candidate``, ``boundary``,
``disown`` and the rest appear in operator-facing log lines, in the phase
contracts handed to workers, and in error messages — always with a precise
sense a reader cannot recover from context. A *packet* is the authored bd
issue content a worker treats as authoritative, not a message on a queue; a
*boundary* is one journaled finalization step, not a limit.

The terms are declared here and rendered into ``README.md`` between the
``glossary`` generated markers by :func:`render_readme_block`, exactly the
way :mod:`ortus.core.lifecycle` renders the state graphs.
``tests/test_glossary_docs.py`` fails when the committed README block and
this declaration disagree, so a definition cannot drift from the behavior it
describes.

This module is documentation. Nothing on the runtime path imports it, so a
wording change cannot affect a run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "TERMS",
    "GlossaryError",
    "Term",
    "readme_block",
    "render_glossary_table",
    "render_readme_block",
    "sort_key",
]


class GlossaryError(ValueError):
    """Raised when the declaration or the generated block is inconsistent."""


@dataclass(frozen=True)
class Term:
    """One glossary entry.

    `definition` is a single sentence describing what the word means to a
    reader of Ortus's output rather than to its implementer, because
    operator-facing output is where these words are first met. `home` names
    the module, symbol or journal field that owns the term, so a reader can
    move from the word to the code without searching for it.
    """

    term: str
    definition: str
    home: str


def sort_key(term: str) -> str:
    """Order terms by their words, so hyphenation cannot move a row.

    `plan-gap` and `main path` have to land where a reader looking the word
    up expects them, not wherever the punctuation happens to sort.
    """

    return term.lower().replace("-", " ")


#: Ortus's vocabulary, alphabetically. Any grouping by subsystem would only
#: invite an argument about which group a word belongs to, and helps nobody
#: looking a word up. This declaration is the source of truth for spelling,
#: including hyphenation: `plan-gap` and `tracker export` appear in logs
#: exactly as they are glossed here.
TERMS: tuple[Term, ...] = (
    Term(
        term="boundary",
        definition=(
            "One finalization step that is journaled as it lands, so a restart "
            "resumes at the first step that did not — not a limit or an edge."
        ),
        home="`FINALIZATION_STEPS` in `src/ortus/core/lifecycle.py`",
    ),
    Term(
        term="candidate",
        definition=(
            "The uncommitted edit set one worker produced for one issue, which "
            "a fresh verifier judges before anything is committed."
        ),
        home="`CandidateJournal.candidate_paths` in `src/ortus/core/transaction.py`",
    ),
    Term(
        term="degraded",
        definition=(
            "A step that completed with less information than usual instead of "
            "failing, such as a commit subject written without a readable packet."
        ),
        home="finalization logging in `src/ortus/commands/grind.py`",
    ),
    Term(
        term="disown",
        definition=(
            "A worker declaring that an inherited uncommitted path is not its "
            "issue's work, which keeps the path out of the candidate rather "
            "than merely leaving it alone."
        ),
        home="`src/ortus/core/attribution.py`",
    ),
    Term(
        term="finalization",
        definition=(
            "The commit-and-close sequence grind runs itself after a passing "
            "verdict, one journaled boundary at a time; no worker closes an issue."
        ),
        home="`finalized_phase()` in `src/ortus/core/lifecycle.py`",
    ),
    Term(
        term="handoff",
        definition=(
            "The uncommitted paths a fresh worker inherits from whoever edited "
            "the tree before it, recorded so attribution can tell them apart "
            "from the worker's own edits."
        ),
        home="`CandidateJournal.with_handoff()` in `src/ortus/core/transaction.py`",
    ),
    Term(
        term="harness",
        definition=(
            "The grind scheduler process that selects and claims the issue and "
            "launches each worker against it; the worker never chooses its own work."
        ),
        home="`src/ortus/core/grind_loop.py`",
    ),
    Term(
        term="journal",
        definition=(
            "The one JSON file holding a candidate transaction's phase, paths, "
            "hashes and evidence, which is what lets an interrupted run resume."
        ),
        home="`JOURNAL_RELATIVE_PATH` in `src/ortus/core/transaction.py`",
    ),
    Term(
        term="leaf",
        definition=(
            "A non-epic bd issue small and complete enough for one implementation "
            "worker to execute end to end, which is what readiness validates."
        ),
        home="`src/ortus/core/readiness.py`",
    ),
    Term(
        term="main path",
        definition=(
            "The route through a state machine taken when nothing goes wrong, "
            "which is the only part the README diagrams draw."
        ),
        home="`StateMachine.main_path` in `src/ortus/core/lifecycle.py`",
    ),
    Term(
        term="orphan",
        definition=(
            "An issue left claimed but unclosed by a worker that ended without "
            "finishing, which the configured orphan policy then releases or keeps."
        ),
        home="`src/ortus/core/grind_loop.py`",
    ),
    Term(
        term="packet",
        definition=(
            "The authored bd issue content — description, design, acceptance "
            "criteria, notes — that a worker treats as authoritative, not any "
            "message on a queue."
        ),
        home="`authoritative_packet()` in `src/ortus/core/transaction.py`",
    ),
    Term(
        term="phase",
        definition=(
            "The candidate journal's current state, which lives only as long as "
            "one candidate transaction and is never a bd issue status."
        ),
        home="`CandidateJournal.phase` in `src/ortus/core/transaction.py`",
    ),
    Term(
        term="plan-gap",
        definition=(
            "A defect in the issue packet that no amount of implementing can "
            "resolve, which routes back to planning instead of producing a candidate."
        ),
        home="`PLAN_GAP_ROUTED` in `src/ortus/core/lifecycle.py`",
    ),
    Term(
        term="readiness",
        definition=(
            "The schema an issue must satisfy before an implementation worker may "
            "be launched at it, checked mechanically when the issue is planned."
        ),
        home="`validate_issue()` in `src/ortus/core/readiness.py`",
    ),
    Term(
        term="seal",
        definition=(
            "Recording the candidate's diff hash, so every later phase can prove "
            "the edit set it is judging is the one the worker produced."
        ),
        home="`CandidateJournal.candidate_hash` in `src/ortus/core/transaction.py`",
    ),
    Term(
        term="tracker export",
        definition=(
            "The generated beads files under `.beads/` that bd rewrites whenever "
            "an issue changes, checkpointed apart from a worker's own edits."
        ),
        home="`src/ortus/commands/grind.py`",
    ),
    Term(
        term="verdict",
        definition=(
            "The structured pass-or-fail judgement a fresh read-only verifier "
            "emits about a candidate, with one entry per acceptance criterion."
        ),
        home="`parse_verdict()` in `src/ortus/core/verdict.py`",
    ),
    Term(
        term="worker",
        definition=(
            "One agent subprocess running one phase for one issue, started fresh "
            "with no memory of any worker before it."
        ),
        home="`compose_worker_prompt()` in `src/ortus/core/agent.py`",
    ),
)


BEGIN_MARKER = "<!-- BEGIN GENERATED: glossary -->"
END_MARKER = "<!-- END GENERATED: glossary -->"


def _cell(value: str) -> str:
    """Escape a table cell, so a definition may contain a pipe.

    An unescaped `|` inside a cell silently splits the row into an extra
    column, which corrupts every row after it in the rendered table.
    """

    return value.replace("|", r"\|")


def render_glossary_table(terms: Sequence[Term] = TERMS) -> str:
    """Render `terms` as the Markdown table README carries.

    A term declared twice is a hard error rather than a silently deduplicated
    or doubled row: two entries for one word means two definitions were
    written, and only a human can say which is right. Order follows the
    declaration, which must already be alphabetical by :func:`sort_key` —
    checked here so the list cannot quietly stop being sorted.
    """

    seen: set[str] = set()
    for entry in terms:
        if entry.term in seen:
            raise GlossaryError(
                f"glossary declares the term {entry.term!r} more than once; "
                "each term needs exactly one definition"
            )
        seen.add(entry.term)

    ordered = sorted(terms, key=lambda entry: sort_key(entry.term))
    if list(ordered) != list(terms):
        expected = ", ".join(entry.term for entry in ordered)
        raise GlossaryError(
            "glossary terms are declared out of alphabetical order; expected: "
            f"{expected}"
        )

    rows = [
        "| Term | What it means | Where it lives |",
        "| --- | --- | --- |",
    ]
    for entry in terms:
        rows.append(
            f"| **{_cell(entry.term)}** | {_cell(entry.definition)} | "
            f"{_cell(entry.home)} |"
        )
    return "\n".join(rows)


def render_readme_block() -> str:
    """The exact text README carries between the glossary markers.

    Nothing here reads the clock or the environment, so two runs in one
    session produce identical output.
    """

    return "\n".join(
        [
            "<!-- Generated from src/ortus/core/glossary.py. Do not edit by hand: "
            "tests/test_glossary_docs.py fails and prints the correct block. -->",
            "",
            render_glossary_table(),
        ]
    )


def readme_block(text: str) -> str:
    """Extract the generated glossary block from README `text`.

    Raises :class:`GlossaryError` with an actionable message naming the marker
    when the markers are missing, duplicated or out of order, rather than
    returning a confusing diff.
    """

    for marker, label in ((BEGIN_MARKER, "begin"), (END_MARKER, "end")):
        count = text.count(marker)
        if count == 0:
            raise GlossaryError(
                f"README is missing the glossary {label} marker: {marker}"
            )
        if count > 1:
            raise GlossaryError(
                f"README has {count} glossary {label} markers; "
                f"expected exactly one: {marker}"
            )
    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        raise GlossaryError(
            "README glossary markers are out of order: "
            f"{END_MARKER} appears before {BEGIN_MARKER}"
        )
    return text[start:end].strip("\n")
