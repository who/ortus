"""Tests for three-layer prompt resolution (xvel.3 acceptance #3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.prompts import (
    PROMPT_REGISTRY,
    READINESS_SPEC_PLACEHOLDER,
    PromptNotFound,
    prompts_in_package,
    registry_entry,
    resolve_named_prompt,
    resolve_prompt,
    substitute,
)
from ortus.core.readiness import spec_markdown


def _write(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_bundled_goal_prompt_resolves_by_default(tmp_path: Path) -> None:
    """No repo or user override; bundled prompt wins."""
    result = resolve_prompt("goal-prompt", repo=tmp_path, home=tmp_path / "home")
    assert result.source == "bundled"
    assert "One context window, one issue" in result.text


def test_bundled_plan_prompt_resolves_by_default(tmp_path: Path) -> None:
    result = resolve_prompt("plan-prompt", repo=tmp_path, home=tmp_path / "home")
    assert result.source == "bundled"
    assert "Decompose the provided PRD" in result.text
    # The contract itself is generated; the bundled file only carries the slot.
    assert READINESS_SPEC_PLACEHOLDER in result.text
    assembled = substitute(result.text, readiness_spec=spec_markdown())
    for heading in (
        "## Scope",
        "## Non-goals",
        "## Concrete locations",
        "## Resolved decisions",
        "## Ordered steps",
        "## Dependencies",
        "## Edge cases",
        "## Criterion checks",
        "## Targeted tests",
    ):
        assert heading in assembled
    assert "Complete executable-task example" in result.text


def test_substitute_fills_the_placeholder_and_spares_shell_dollars() -> None:
    """The prompts are shell-heavy; only the named placeholder may change."""
    text = (
        "ID=$(bd create --silent)\n"
        "$readiness_spec\n"
        'jq -r --arg t "$title" \'.[] | select(.title == $t)\'\n'
    )
    filled = substitute(text, readiness_spec="CONTRACT")
    assert "CONTRACT" in filled
    assert READINESS_SPEC_PLACEHOLDER not in filled
    assert "$(bd create --silent)" in filled
    assert "$t" in filled and '"$title"' in filled


def test_stale_override_missing_placeholder_still_substitutes(tmp_path: Path) -> None:
    """A private override copied before the placeholder must run, not raise."""
    home = tmp_path / "home"
    _write(
        home / ".ortus" / "prompts" / "plan-prompt.md",
        "FROZEN CONTRACT $(bd create) $ID_FEATURE_A ${unset} 50$ raw\n",
    )
    result = resolve_prompt("plan-prompt", repo=tmp_path / "repo", home=home)
    assert result.source == "user"
    assert substitute(result.text, readiness_spec=spec_markdown()) == result.text


def test_user_layer_overrides_bundled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".ortus" / "prompts" / "goal-prompt.md", "USER-LEVEL-SENTINEL")
    result = resolve_prompt("goal-prompt", repo=tmp_path / "repo", home=home)
    assert result.source == "user"
    assert result.text == "USER-LEVEL-SENTINEL"


def test_repo_layer_overrides_user_and_bundled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _write(home / ".ortus" / "prompts" / "goal-prompt.md", "USER-LEVEL-SENTINEL")
    _write(repo / ".ortus" / "prompts" / "goal-prompt.md", "REPO-LEVEL-SENTINEL")
    result = resolve_prompt("goal-prompt", repo=repo, home=home)
    assert result.source == "repo"
    assert result.text == "REPO-LEVEL-SENTINEL"


def test_missing_prompt_raises(tmp_path: Path) -> None:
    with pytest.raises(PromptNotFound):
        resolve_prompt(
            "no-such-prompt-name-anywhere",
            repo=tmp_path,
            home=tmp_path / "home",
        )


def test_repo_none_skips_repo_layer(tmp_path: Path) -> None:
    """When repo=None, only user + bundled are checked (per design)."""
    home = tmp_path / "home"
    _write(home / ".ortus" / "prompts" / "goal-prompt.md", "USER-WINS-WHEN-NO-REPO")
    result = resolve_prompt("goal-prompt", repo=None, home=home)
    assert result.source == "user"
    assert result.text == "USER-WINS-WHEN-NO-REPO"


# --- prompt registry gate (ortus-apv5.1) ------------------------------------


def test_registry_reconciles_with_bundled_package(tmp_path: Path) -> None:
    """Every bundled *.md has exactly one registry entry, and vice versa."""
    stems = prompts_in_package()
    assert sorted(entry.filename for entry in PROMPT_REGISTRY) == sorted(stems)
    names = [entry.name for entry in PROMPT_REGISTRY]
    assert len(set(names)) == len(names)
    for entry in PROMPT_REGISTRY:
        resolved = resolve_prompt(
            entry.filename, repo=tmp_path, home=tmp_path / "home"
        )
        assert resolved.source == "bundled"
        assert resolved.text.strip()


def test_resolve_named_prompt_is_a_thin_alias(tmp_path: Path) -> None:
    """`goal` resolves to the same prompt as the `goal-prompt` stem."""
    home = tmp_path / "home"
    named = resolve_named_prompt("goal", repo=tmp_path, home=home)
    stem = resolve_prompt("goal-prompt", repo=tmp_path, home=home)
    assert named == stem


def test_registry_entry_unknown_name_lists_valid_names() -> None:
    with pytest.raises(PromptNotFound) as exc:
        registry_entry("no-such-prompt")
    message = str(exc.value)
    for entry in PROMPT_REGISTRY:
        assert entry.name in message
