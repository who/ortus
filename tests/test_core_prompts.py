"""Tests for three-layer prompt resolution (xvel.3 acceptance #3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.prompts import PromptNotFound, resolve_prompt


def _write(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_bundled_grind_prompt_resolves_by_default(tmp_path: Path) -> None:
    """No repo or user override; bundled prompt wins."""
    result = resolve_prompt("grind-prompt", repo=tmp_path, home=tmp_path / "home")
    assert result.source == "bundled"
    assert "Grind Loop Prompt" in result.text


def test_bundled_plan_prompt_resolves_by_default(tmp_path: Path) -> None:
    result = resolve_prompt("plan-prompt", repo=tmp_path, home=tmp_path / "home")
    assert result.source == "bundled"
    assert "Decompose the provided PRD" in result.text


def test_user_layer_overrides_bundled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".ortus" / "prompts" / "grind-prompt.md", "USER-LEVEL-SENTINEL")
    result = resolve_prompt("grind-prompt", repo=tmp_path / "repo", home=home)
    assert result.source == "user"
    assert result.text == "USER-LEVEL-SENTINEL"


def test_repo_layer_overrides_user_and_bundled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _write(home / ".ortus" / "prompts" / "grind-prompt.md", "USER-LEVEL-SENTINEL")
    _write(repo / ".ortus" / "prompts" / "grind-prompt.md", "REPO-LEVEL-SENTINEL")
    result = resolve_prompt("grind-prompt", repo=repo, home=home)
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
    _write(home / ".ortus" / "prompts" / "grind-prompt.md", "USER-WINS-WHEN-NO-REPO")
    result = resolve_prompt("grind-prompt", repo=None, home=home)
    assert result.source == "user"
    assert result.text == "USER-WINS-WHEN-NO-REPO"
