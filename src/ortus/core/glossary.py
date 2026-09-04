"""Ortus's vocabulary, declared once as data.

Ortus's words appear in operator-facing log lines, in the CodeGraph
contracts handed to workers, and in error messages — always with a precise
sense a reader cannot recover from context alone. Most are standard
software-engineering vocabulary carrying one Ortus-specific sense: a *work
spec* is the authored bd issue content a worker treats as authoritative, a
*session-close* is the worker's own commit, close and push at the end of
one issue.

Naming bar
----------

A new coinage must state why no standard software-engineering term fits
before it may join this glossary. When an established term carries the
meaning, the established term wins and its entry records only the one
Ortus-specific sense it is used in; novelty is spent solely on concepts
that are themselves genuinely novel.

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
    #: The same idea on a team that has never run an agent: which person,
    #: artifact or ceremony plays this part. Ortus's vocabulary is unfamiliar
    #: but the machinery is not — nearly every term is a role a team already
    #: fills, so naming that role is the shortest route from "what is this
    #: word" to "oh, I know what that is."
    team_role: str
    #: The same idea with no software in it at all, for a reader who has
    #: never sat on a delivery team. A word is understood when it lands
    #: against something already known, and not everyone's something is a
    #: sprint board.
    analogy: str


def sort_key(term: str) -> str:
    """Order terms by their words, so hyphenation cannot move a row.

    A hyphenated coinage has to land where a reader looking the word up
    expects it, not wherever the punctuation happens to sort.
    """

    return term.lower().replace("-", " ")


#: Ortus's vocabulary, alphabetically. Any grouping by subsystem would only
#: invite an argument about which group a word belongs to, and helps nobody
#: looking a word up. This declaration is the source of truth for spelling,
#: including hyphenation: `planning gap` and `session-close` appear in logs
#: exactly as they are glossed here.
TERMS: tuple[Term, ...] = (
    Term(
        term="orphan",
        definition=(
            "An issue left claimed but unclosed by a worker that ended without "
            "finishing, which the configured orphan policy then releases or keeps."
        ),
        home="`src/ortus/core/grind_loop.py`",
        team_role=(
            "A ticket left In Progress by someone who went on holiday without "
            "updating the board."
        ),
        analogy=(
            "A library book still on loan to someone who has left town and is "
            "not coming back for it."
        ),
    ),
    Term(
        term="planning gap",
        definition=(
            "A defect in the work spec that no amount of implementing can "
            "resolve, which routes back to planning instead of shipping the issue."
        ),
        home="`plan_gap_guidance` in `src/ortus/core/readiness.py`",
        team_role=(
            "A developer handing a ticket back to the analyst because it cannot "
            "be built as written."
        ),
        analogy=(
            "A builder downing tools because the blueprint gives no dimension "
            "for a wall. No amount of building resolves it."
        ),
    ),
    Term(
        term="readiness",
        definition=(
            "The schema an issue must satisfy before an implementation worker may "
            "be launched at it, checked mechanically when the issue is planned."
        ),
        home="`validate_issue()` in `src/ortus/core/readiness.py`",
        team_role=(
            "Definition of Ready: the checklist a story passes before planning "
            "will let anyone start it."
        ),
        analogy=(
            "The pre-flight checklist an aircraft passes before pushback, not "
            "an opinion about whether it looks ready."
        ),
    ),
    Term(
        term="session-close",
        definition=(
            "The worker's own commit, bd close, bd dolt push and git push at "
            "the end of one issue, after which grind reaps."
        ),
        home="`src/ortus/prompts/goal-prompt.md` step 4",
        team_role=(
            "The developer closing their own ticket after the checks they ran, "
            "not a release manager doing it for them."
        ),
        analogy=(
            "The couple signing their own register. The registrar is not in "
            "the room."
        ),
    ),
    Term(
        term="task",
        definition=(
            "A non-epic bd issue small and complete enough for one implementation "
            "worker to execute end to end, which is what readiness validates."
        ),
        home="`src/ortus/core/readiness.py`",
        team_role=(
            "A story an engineer can finish in one sitting, as opposed to an "
            "epic that has to be broken down first."
        ),
        analogy=(
            "An errand you can finish on one trip, rather than a house move "
            "that has to be broken into trips first."
        ),
    ),
    Term(
        term="work spec",
        definition=(
            "The authored bd issue content (description, design, acceptance "
            "criteria, notes) that a worker treats as authoritative, not any "
            "message on a queue."
        ),
        home="`src/ortus/core/readiness.py`",
        team_role=(
            "The ticket as the analyst wrote it: the spec of record a developer "
            "builds from and argues with, not a chat message."
        ),
        analogy=(
            "The blueprint handed to the builder. What is on the paper governs, "
            "not what anyone remembers saying."
        ),
    ),
    Term(
        term="worker",
        definition=(
            "One agent subprocess that implements one issue end to end, "
            "including its acceptance checks and session-close, started fresh "
            "with no memory of any worker before it."
        ),
        home="`compose_worker_prompt()` in `src/ortus/core/agent.py`",
        team_role=(
            "A contractor hired for exactly one ticket, who has never seen the "
            "codebase before and will not be back."
        ),
        analogy=(
            "A temp who works exactly one shift, has never seen the building "
            "before, and will not be back tomorrow."
        ),
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
        "| Term | What it means | On a team without agents | Analogy | "
        "Where it lives |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in terms:
        rows.append(
            f"| **{_cell(entry.term)}** | {_cell(entry.definition)} | "
            f"{_cell(entry.team_role)} | {_cell(entry.analogy)} | "
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
