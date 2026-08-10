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
    LOG_LABELS,
    LifecycleError,
    mermaid_candidate_graph,
    mermaid_issue_graph,
    readme_block,
    render_readme_block,
)

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_contains_both_graphs() -> None:
    block = readme_block(_readme_text())

    assert block.count("```mermaid") == 2
    assert mermaid_issue_graph() in block
    assert mermaid_candidate_graph() in block
    assert ISSUE_MACHINE.title in block
    assert CANDIDATE_MACHINE.title in block

    # The coupling prose is part of the section, not an afterthought.
    for coupling in COUPLINGS:
        assert coupling.description in block
    for label in LOG_LABELS:
        assert label in block


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
        for state in machine.states:
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
        "#### Where the two machines meet",
        "#### Where the two machines meet (hand-edited)",
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
