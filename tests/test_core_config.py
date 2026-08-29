"""Tests for core/config.py — layered .ortusrc resolution (q075.3 acceptance #3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.config import (
    DEFAULT_CODEGRAPH_MODE,
    DEFAULT_MERGE_GATE_TIMEOUT,
    DEFAULTS,
    load_config,
)
from ortus.core.profiles import Phase, ProfileError


def _write_toml(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_only_defaults_when_no_files(tmp_path: Path) -> None:
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert cfg.values == DEFAULTS
    assert [layer.source for layer in cfg.layers] == ["defaults"]


def test_user_layer_overrides_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_toml(home / ".ortusrc", 'owner = "user-owner"\n')
    cfg = load_config(repo=tmp_path / "repo-with-no-rc", home=home)
    assert cfg.values["owner"] == "user-owner"
    assert [layer.source for layer in cfg.layers] == ["defaults", "user"]


def test_project_layer_overrides_user(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_toml(home / ".ortusrc", 'owner = "user-owner"\nprefix = "user-prefix"\n')
    _write_toml(repo / ".ortusrc", 'owner = "repo-owner"\n')
    cfg = load_config(repo=repo, home=home)
    # project wins for owner; user remains for prefix
    assert cfg.values["owner"] == "repo-owner"
    assert cfg.values["prefix"] == "user-prefix"
    assert [layer.source for layer in cfg.layers] == ["defaults", "user", "project"]


def test_repo_none_skips_project_layer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_toml(home / ".ortusrc", 'owner = "user-owner"\n')
    cfg = load_config(repo=None, home=home)
    assert cfg.values["owner"] == "user-owner"
    assert [layer.source for layer in cfg.layers] == ["defaults", "user"]


def test_config_get_returns_default_for_missing_key(tmp_path: Path) -> None:
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert cfg.get("nope", "fallback") == "fallback"
    assert cfg.get("owner") is None


@pytest.mark.codegraph_default
def test_codegraph_default_is_required(tmp_path: Path) -> None:
    """AC-1: no `codegraph` key in any layer resolves to `required`."""
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert DEFAULT_CODEGRAPH_MODE == "required"
    assert cfg.get("codegraph") == "required"


def test_merge_gate_defaults_off(tmp_path: Path) -> None:
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert cfg.get("merge_gate") is False
    assert cfg.get("merge_gate_timeout") == DEFAULT_MERGE_GATE_TIMEOUT


def test_merge_gate_project_pin_wins(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_toml(repo / ".ortusrc", "merge_gate = true\nmerge_gate_timeout = 90\n")
    cfg = load_config(repo=repo, home=tmp_path / "home")
    assert cfg.get("merge_gate") is True
    assert cfg.get("merge_gate_timeout") == 90


def test_integration_branch_defaults_to_main(tmp_path: Path) -> None:
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert cfg.get("integration_branch") == "main"


def test_integration_branch_project_pin_wins(tmp_path: Path) -> None:
    """A repo whose default branch isn't 'main' pins it once in .ortusrc."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_toml(repo / ".ortusrc", 'integration_branch = "master"\n')
    cfg = load_config(repo=repo, home=tmp_path / "home")
    assert cfg.get("integration_branch") == "master"


def test_codegraph_explicit_pin_still_wins(tmp_path: Path) -> None:
    """A project that pins a value is unaffected by the default flip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_toml(repo / ".ortusrc", 'codegraph = "auto"\n')
    cfg = load_config(repo=repo, home=tmp_path / "home")
    assert cfg.get("codegraph") == "auto"


def test_round_trip_sample_rc(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_toml(
        home / ".ortusrc",
        'owner = "alice"\nprefix = "feat"\ncondition = "queue empty"\n',
    )
    cfg = load_config(repo=tmp_path / "no-repo-rc", home=home)
    assert cfg.values["owner"] == "alice"
    assert cfg.values["prefix"] == "feat"
    assert cfg.values["condition"] == "queue empty"


def test_home_defaults_to_path_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When home=None, defaults to Path.home() — exercise the default branch."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    cfg = load_config(repo=None)
    assert [layer.source for layer in cfg.layers] == ["defaults"]


def test_profiles_merge_partial_nested_layers_and_phases(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_toml(
        home / ".ortusrc",
        '[profiles.claude.plan]\nmodel = "sonnet"\nreasoning_effort = "medium"\n'
        '[profiles.claude.verify]\nmodel = "opus"\n',
    )
    _write_toml(
        repo / ".ortusrc",
        '[profiles.claude.plan]\nreasoning_effort = "high"\n'
        '[profiles.claude.implement]\nmodel = "haiku"\n',
    )
    cfg = load_config(repo=repo, home=home)
    assert cfg.resolve_profile("claude", Phase.PLAN).model == "sonnet"
    assert cfg.resolve_profile("claude", Phase.PLAN).reasoning_effort == "high"
    assert cfg.resolve_profile("claude", Phase.IMPLEMENT).model == "haiku"
    assert cfg.resolve_profile("claude", Phase.VERIFY).model == "opus"


def test_profile_cli_fields_override_config_independently(tmp_path: Path) -> None:
    _write_toml(
        tmp_path / ".ortusrc",
        '[profiles.codex.plan]\nmodel = "configured"\nreasoning_effort = "medium"\n',
    )
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    profile = cfg.resolve_profile("codex", Phase.PLAN, model="cli-model")
    assert profile.model == "cli-model"
    assert profile.reasoning_effort == "medium"


def test_finalize_phase_profile_is_settable_for_both_backends(tmp_path: Path) -> None:
    """The commit-message pass is an operator-tunable phase like any other."""

    _write_toml(
        tmp_path / ".ortusrc",
        '[profiles.claude.finalize]\nmodel = "haiku"\nreasoning_effort = "low"\n'
        '[profiles.codex.finalize]\nmodel = "cheap-codex"\n',
    )
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")

    claude = cfg.resolve_profile("claude", Phase.FINALIZE)
    assert (claude.model, claude.reasoning_effort) == ("haiku", "low")
    assert cfg.resolve_profile("codex", Phase.FINALIZE).model == "cheap-codex"


def test_finalize_phase_profile_is_named_in_the_error_for_an_unknown_phase(
    tmp_path: Path,
) -> None:
    _write_toml(tmp_path / ".ortusrc", '[profiles.claude.ship]\nmodel = "x"\n')
    with pytest.raises(ProfileError, match="expected plan, implement, verify, finalize"):
        load_config(repo=tmp_path, home=tmp_path / "home")


@pytest.mark.parametrize(
    "toml, message",
    [
        ('[profiles.other.plan]\nmodel = "x"\n', "profile backend"),
        ('[profiles.claude.plan]\nmodel = ""\n', "invalid model"),
        ('[profiles.codex.verify]\nreasoning_effort = "max"\n', "reasoning_effort"),
    ],
)
def test_invalid_profile_configuration_is_actionable(
    tmp_path: Path, toml: str, message: str
) -> None:
    _write_toml(tmp_path / ".ortusrc", toml)
    with pytest.raises(ProfileError, match=message):
        load_config(repo=tmp_path, home=tmp_path / "home")
