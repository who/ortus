"""README's glossary block must be exactly what the declaration renders."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ortus.core import lifecycle
from ortus.core.glossary import (
    BEGIN_MARKER,
    END_MARKER,
    TERMS,
    GlossaryError,
    Term,
    readme_block,
    render_glossary_table,
    render_readme_block,
    sort_key,
)

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"

#: The vocabulary this glossary owes a definition: the words that appear in
#: operator-facing output, prompt contracts or error messages. Restated here
#: rather than derived from the declaration, so dropping a term from
#: `TERMS` fails instead of quietly shrinking what is documented.
REQUIRED_TERMS = frozenset(
    {
        "orphan",
        "planning gap",
        "readiness",
        "session-close",
        "task",
        "work spec",
        "worker",
    }
)


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_contains_the_glossary() -> None:
    text = _readme_text()

    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1

    block = readme_block(text)
    # The whole separator, not a prefix of it: a shorter string would still
    # match inside a wider table and stop noticing a dropped column.
    assert block.count("| --- | --- | --- | --- | --- |") == 1

    # Its own marker pair, next to the state graphs and never inside them: a
    # glossary edit and a state-machine edit must not collide in one block.
    assert lifecycle.BEGIN_MARKER not in block
    assert lifecycle.END_MARKER not in block
    assert BEGIN_MARKER not in lifecycle.readme_block(text)
    assert END_MARKER not in lifecycle.readme_block(text)


def test_state_graph_block_is_untouched() -> None:
    """The section this one sits beside stays byte-identical to its renderer."""

    assert lifecycle.readme_block(_readme_text()) == lifecycle.render_readme_block()


def _assert_block_matches(text: str) -> None:
    """The parity check itself, so a test can execute its failure path.

    Raised rather than asserted for the same reason the state-graph test
    raises: pytest rewrites a bare `assert a == b, msg` into an explanation it
    truncates without `-vv`, which would cut the regenerated block in half and
    leave the fix un-copyable.
    """

    expected = render_readme_block()
    actual = readme_block(text)
    if actual != expected:
        raise AssertionError(
            "README.md's glossary block has drifted from "
            "src/ortus/core/glossary.py.\n"
            f"Replace everything between {BEGIN_MARKER} and {END_MARKER} with:\n\n"
            f"{expected}\n"
        )


def test_readme_block_matches_renderer() -> None:
    _assert_block_matches(_readme_text())


def test_hand_edit_inside_the_markers_is_detected() -> None:
    tampered = _readme_text().replace(
        "| **work spec** |", "| **work spec (hand-edited)** |", 1
    )
    assert tampered != _readme_text()

    with pytest.raises(AssertionError) as caught:
        _assert_block_matches(tampered)
    message = str(caught.value)
    assert "has drifted from" in message
    assert BEGIN_MARKER in message
    assert END_MARKER in message
    assert render_readme_block() in message


def test_every_declared_term_is_rendered() -> None:
    block = readme_block(_readme_text())
    declared = {entry.term for entry in TERMS}

    assert declared == REQUIRED_TERMS, (
        "the glossary declaration and the vocabulary it owes definitions for "
        f"disagree: {declared ^ REQUIRED_TERMS}"
    )
    for entry in TERMS:
        row = (
            f"| **{entry.term}** | {entry.definition} | {entry.team_role} | "
            f"{entry.analogy} | {entry.home} |"
        )
        assert row in block, f"{entry.term} is declared but absent from the README"


def test_each_definition_is_one_sentence_naming_where_it_lives() -> None:
    for entry in TERMS:
        assert entry.definition.endswith("."), entry.term
        # A term needing a paragraph belongs in prose elsewhere, with the
        # glossary pointing at it. `.beads/` keeps this off a naive period count.
        assert ". " not in entry.definition, entry.term
        assert entry.home, entry.term
        assert entry.definition[0].isupper(), entry.term


def test_duplicate_term_is_rejected() -> None:
    duplicated = (*TERMS, dataclasses.replace(TERMS[0], definition="Something else."))

    with pytest.raises(GlossaryError, match="more than once"):
        render_glossary_table(duplicated)


def test_terms_are_declared_alphabetically() -> None:
    assert [entry.term for entry in TERMS] == sorted(
        (entry.term for entry in TERMS), key=sort_key
    )

    # Hyphens and spaces must not push a row away from where a reader looks
    # for it: `plan-gap` sorts under "plan gap", after `phase`.
    assert sort_key("plan-gap") == "plan gap"
    assert sorted(["phase", "plan-gap", "packet"], key=sort_key) == [
        "packet",
        "phase",
        "plan-gap",
    ]

    swapped = (TERMS[1], TERMS[0], *TERMS[2:])
    with pytest.raises(GlossaryError, match="out of alphabetical order"):
        render_glossary_table(swapped)


def test_a_pipe_in_a_definition_cannot_break_the_table() -> None:
    rendered = render_glossary_table(
        (
            Term(
                term="pipe",
                definition="A | in a cell.",
                home="`nowhere`",
                team_role="A | in the team role too.",
                analogy="A | in the analogy too.",
            ),
        )
    )
    row = rendered.splitlines()[-1]

    assert r"A \| in a cell." in row
    assert r"A \| in the team role too." in row
    assert r"A \| in the analogy too." in row
    # Escaped, the row still has exactly the five declared columns.
    assert row.replace(r"\|", "").count("|") == 6


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no markers here", "missing the glossary begin marker"),
        (f"{BEGIN_MARKER}\nbody\n", "missing the glossary end marker"),
        (
            f"{BEGIN_MARKER}\na\n{END_MARKER}\n{BEGIN_MARKER}\nb\n{END_MARKER}\n",
            "expected exactly one",
        ),
        (f"{END_MARKER}\nbody\n{BEGIN_MARKER}\n", "out of order"),
    ],
)
def test_marker_errors_are_actionable(text: str, message: str) -> None:
    with pytest.raises(GlossaryError, match=message) as caught:
        readme_block(text)

    # Actionable means the message names the marker to go fix.
    assert BEGIN_MARKER in str(caught.value) or END_MARKER in str(caught.value)


def test_renderer_is_deterministic() -> None:
    assert render_readme_block() == render_readme_block()
    assert render_glossary_table() == render_glossary_table()


def test_no_runtime_module_imports_the_glossary() -> None:
    """Documentation must not be able to affect a run."""

    offenders = [
        path.relative_to(REPO).as_posix()
        for path in sorted((REPO / "src" / "ortus").rglob("*.py"))
        if path.name != "glossary.py" and "glossary" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], (
        "the glossary module is documentation and nothing on the runtime path "
        f"may import it; found references in {offenders}"
    )
