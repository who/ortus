"""Tests for core/hooks.py — disableAllHooks precheck (q075.3 acceptance #4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ortus.core.hooks import (
    HookConflictError,
    _candidate_layers,
    _managed_settings_path,
    check_hooks_enabled,
)


def _write_settings(p: Path, payload: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload))


def test_no_settings_files_passes(tmp_path: Path) -> None:
    result = check_hooks_enabled(tmp_path / "repo", home=tmp_path / "home")
    assert result.checked_paths == ()


def test_hooks_enabled_explicitly_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_settings(repo / ".claude" / "settings.json", {"disableAllHooks": False})
    result = check_hooks_enabled(repo, home=tmp_path / "home")
    assert len(result.checked_paths) == 1


def test_disabled_in_repo_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    settings = repo / ".claude" / "settings.json"
    _write_settings(settings, {"disableAllHooks": True})
    with pytest.raises(HookConflictError) as exc:
        check_hooks_enabled(repo, home=tmp_path / "home")
    assert exc.value.settings_path == settings
    assert "disableAllHooks=true" in str(exc.value)


def test_disabled_in_user_raises(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    _write_settings(settings, {"disableAllHooks": True})
    with pytest.raises(HookConflictError) as exc:
        check_hooks_enabled(tmp_path / "repo", home=home)
    assert exc.value.settings_path == settings


def test_malformed_json_is_silently_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not valid json")
    # Should not raise — mirrors goal.sh's silent-on-parse-fail behavior.
    result = check_hooks_enabled(repo, home=tmp_path / "home")
    assert settings in result.checked_paths


def test_managed_layer_included_on_supported_platforms() -> None:
    candidates = _candidate_layers(Path("/some/repo"), Path("/some/home"))
    # Always includes at minimum user + repo settings.
    assert Path("/some/home/.claude/settings.json") in candidates
    assert Path("/some/repo/.claude/settings.json") in candidates


def test_managed_settings_path_returns_path_or_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import platform as _platform

    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    assert _managed_settings_path() == Path("/etc/claude/managed-settings.json")
    monkeypatch.setattr(_platform, "system", lambda: "Darwin")
    assert _managed_settings_path() is not None
    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    assert _managed_settings_path() is None


def test_disabled_false_value_passes(tmp_path: Path) -> None:
    """disableAllHooks=true (bool) triggers; non-true values do not."""
    repo = tmp_path / "repo"
    _write_settings(repo / ".claude" / "settings.json", {"disableAllHooks": "true"})
    # String "true" should not trip the check (per goal.sh's strict bool match).
    result = check_hooks_enabled(repo, home=tmp_path / "home")
    assert len(result.checked_paths) == 1


def test_home_defaults_to_path_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When home=None, defaults to Path.home()."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-such-home"))
    result = check_hooks_enabled(tmp_path / "no-repo")
    assert result.checked_paths == ()
