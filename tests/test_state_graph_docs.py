"""README's state-graph block must be exactly what the declaration renders."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.lifecycle import (
    BEGIN_MARKER,
    CANDIDATE_MACHINE,
    COUPLINGS,
    END_MARKER,
    ISSUE_MACHINE,
    LifecycleError,
    mermaid_candidate_graph,
    mermaid_issue_graph,
    readme_block,
    render_readme_block,
)

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_contains_the_issue_graph_only() -> None:
    block = readme_block(_readme_text())

    assert block.count("```mermaid") == 1
    assert mermaid_issue_graph() in block
    assert mermaid_candidate_graph() not in block
    assert ISSUE_MACHINE.title in block
    assert CANDIDATE_MACHINE.title not in block
    assert "Where the two machines meet" not in block
    for coupling in COUPLINGS:
        assert coupling.description not in block


def _assert_block_matches(text: str) -> None:
    """The parity check itself, so a test can execute its failure path.

    Held in one place rather than restated: the drift test below proves the
    message a contributor actually sees, and it can only prove the real one by
    raising from the same code path.

    Raised rather than asserted on purpose. A bare `assert a == b, msg` is
    rewritten by pytest into an explanation it then truncates to a few lines
    unless `-vv` is passed, which would cut the regenerated block in half and
    leave the fix un-copyable — the one thing this message exists to provide.
    """

    expected = render_readme_block()
    actual = readme_block(text)
    if actual != expected:
        raise AssertionError(
            "README.md's state-graph block has drifted from "
            "src/ortus/core/lifecycle.py.\n"
            f"Replace everything between {BEGIN_MARKER} and {END_MARKER} with:\n\n"
            f"{expected}\n"
        )


def test_every_transition_is_documented_even_when_the_diagram_omits_it() -> None:
    """The diagram draws the main path only; the table owes the rest.

    This is the contract that lets the picture stay readable. Drop it and a
    state can be added to the machine, left out of the main path, and never
    documented anywhere — which is worse than the crowded diagram this
    replaced, because the omission is invisible.
    """

    block = readme_block(_readme_text())
    for transition in ISSUE_MACHINE.transitions:
        row = (
            f"| `{transition.source}` | {transition.trigger} | "
            f"`{transition.target}` |"
        )
        assert row in block, (
            f"{ISSUE_MACHINE.name}: {transition.source} -> {transition.target} "
            "is declared but absent from the README transition table"
        )
    for transition in CANDIDATE_MACHINE.transitions:
        row = (
            f"| `{transition.source}` | {transition.trigger} | "
            f"`{transition.target}` |"
        )
        assert row not in block, (
            f"{CANDIDATE_MACHINE.name}: {transition.source} -> "
            f"{transition.target} must not appear in the README block"
        )


def test_readme_block_matches_renderer() -> None:
    _assert_block_matches(_readme_text())


def test_generated_block_is_deterministic() -> None:
    assert render_readme_block() == render_readme_block()


def test_mermaid_state_ids_are_hyphen_free() -> None:
    """Hyphens are not legal in a bare Mermaid state id; they must be aliased."""

    for machine, graph in (
        (ISSUE_MACHINE, mermaid_issue_graph()),
        (CANDIDATE_MACHINE, mermaid_candidate_graph()),
    ):
        assert graph.startswith("stateDiagram-v2")
        # Only the happy path is drawn, so only the happy path needs aliasing;
        # the states left out are carried by the transition table instead.
        for state in machine.happy_path or machine.states:
            if "-" not in state:
                continue
            assert f'state "{state}" as {state.replace("-", "_")}' in graph
            for line in graph.splitlines():
                if "-->" in line:
                    assert state not in line.split(":", 1)[0]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no markers here", "missing the state-graph begin marker"),
        (f"{BEGIN_MARKER}\nbody\n", "missing the state-graph end marker"),
        (
            f"{BEGIN_MARKER}\na\n{END_MARKER}\n{BEGIN_MARKER}\nb\n{END_MARKER}\n",
            "expected exactly one",
        ),
        (f"{END_MARKER}\nbody\n{BEGIN_MARKER}\n", "out of order"),
    ],
)
def test_broken_markers_fail_clearly(text: str, message: str) -> None:
    with pytest.raises(LifecycleError, match=message):
        readme_block(text)


def test_hand_edit_inside_the_markers_is_detected() -> None:
    tampered = _readme_text().replace(
        f"#### {ISSUE_MACHINE.title}",
        f"#### {ISSUE_MACHINE.title} (hand-edited)",
        1,
    )
    assert tampered != _readme_text()
    assert readme_block(tampered) != render_readme_block()

    # Run the assertion a contributor would hit, not a paraphrase of it: the
    # hand-edit has to fail the suite, and the failure has to hand back the
    # regenerated block so the fix is a copy-paste.
    with pytest.raises(AssertionError) as caught:
        _assert_block_matches(tampered)
    message = str(caught.value)
    assert "has drifted from" in message
    assert BEGIN_MARKER in message
    assert END_MARKER in message
    assert render_readme_block() in message
