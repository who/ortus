"""The /goal blocked exit (ortus-urkb).

Claude's /goal condition is a Stop hook: while the done bar is unmet it blocks
the turn from ending. When the only unmet step is outside the worker's reach
(a remote that refuses the push, an operator approval, a history rewrite), the
bar can never be met, and the hook used to re-fire until Claude's block cap
ended the turn. The condition and the goal prompt now name a blocked exit, a
single `BLOCKED:` bd comment, that counts as achieved.
"""

from __future__ import annotations

import re

import pytest

from ortus.commands import grind as grind_mod
from ortus.commands.grind import (
    _CLAUDE_GOAL_CONDITION_LIMIT,
    _GOAL_POINTER,
    _IMPLEMENTATION_INSTRUCTION,
    _LESSON_MAX_CHARS,
    _LESSONS_MAX_COUNT,
    _PROTOTYPE_GOAL_POINTER,
    _compose_work_prompt,
    _lessons_section,
    _prototype_verification_section,
)
from ortus.core.codegraph import (
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    phase_contract,
)
from ortus.core.prompts import bundled_prompt_text

_POINTERS = {
    "full": _GOAL_POINTER,
    "prototype": _PROTOTYPE_GOAL_POINTER,
}


def _goal_prompts() -> dict[str, str]:
    return {
        "legacy": bundled_prompt_text("goal-prompt"),
        "audited": bundled_prompt_text("goal-prompt", audited=True),
    }


def _blocked_sentence(text: str) -> str:
    """The sentence of ``text`` that carries the `BLOCKED:` exit."""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if "BLOCKED:" in sentence:
            return sentence
    raise AssertionError("no `BLOCKED:` sentence in the text")


@pytest.mark.parametrize("name", sorted(_POINTERS))
def test_pointer_carries_the_blocked_exit(name: str) -> None:
    pointer = _POINTERS[name]
    assert "`BLOCKED:`" in pointer
    sentence = _blocked_sentence(pointer)
    assert "achieved" in sentence
    assert "only unmet step" in sentence
    assert "out of your reach" in sentence
    assert "refused push" in sentence
    assert "approval" in sentence
    # The exit follows the done bar instead of replacing it.
    assert pointer.index("Achieved when that issue is closed") < pointer.index(
        "BLOCKED:"
    )


@pytest.mark.parametrize("variant", ["legacy", "audited"])
def test_pointer_goal_prompt_carries_the_blocked_exit(variant: str) -> None:
    text = _goal_prompts()[variant]
    sentence = _blocked_sentence(text)
    assert "cannot fix" in sentence
    assert "refuses the push" in sentence
    # The exit lives in the Exit step, after the done bar it qualifies.
    assert text.index("5. **Exit.**") < text.index("BLOCKED:")


def _max_lessons_text() -> str:
    lessons = tuple(
        (f"lesson-key-{n}-{'k' * 24}", "x" * _LESSON_MAX_CHARS)
        for n in range(_LESSONS_MAX_COUNT)
    )
    return _lessons_section(lessons)


def _required_contract() -> str:
    probe = CodeGraphProbe(CodeGraphMode.REQUIRED, True, True, True)
    return phase_contract(CodeGraphPhase.IMPLEMENTATION, probe)


@pytest.mark.parametrize("prototype", [False, True])
def test_limit_holds_with_a_maximal_lessons_section(prototype: bool) -> None:
    lessons_text = _max_lessons_text()
    verification_text = (
        _prototype_verification_section(("ruff check .",), ("python -m compileall -q .",))
        if prototype
        else ""
    )
    body = _compose_work_prompt(
        "",
        {"id": "repo-1", "title": "an issue"},
        "claude",
        phase_instruction=_IMPLEMENTATION_INSTRUCTION,
        phase_contract_text=_required_contract(),
        verification_text=verification_text,
        lessons_text=lessons_text,
    )
    condition = body.removeprefix("/goal ")
    pointer = _PROTOTYPE_GOAL_POINTER if prototype else _GOAL_POINTER
    assert pointer in condition
    # The lessons section is not dropped to make room for the clause.
    assert lessons_text in condition
    assert len(condition) <= _CLAUDE_GOAL_CONDITION_LIMIT


def test_limit_constant_is_unchanged() -> None:
    assert grind_mod._CLAUDE_GOAL_CONDITION_LIMIT == 4_000


_FORCE_EXIT = re.compile(
    r"--force|push -f|force[- ]push(?:ing)? (?:to|it|the)|amend (?:it|the)|"
    r"rewrite (?:the )?history to",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "name, text",
    [*sorted(_POINTERS.items()), *sorted(_goal_prompts().items())],
)
def test_force_push_is_never_the_exit(name: str, text: str) -> None:
    sentence = _blocked_sentence(text)
    assert "force" not in sentence.lower(), name
    assert "amend" not in sentence.lower(), name
    assert not _FORCE_EXIT.search(text), name


@pytest.mark.parametrize("variant", ["legacy", "audited"])
def test_force_push_ban_survives_in_the_goal_prompt(variant: str) -> None:
    text = _goal_prompts()[variant]
    assert "never an amend or a force-push" in text
