"""Tests for core/config.py — layered .ortusrc resolution (q075.3 acceptance #3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.config import DEFAULTS, load_config


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
    assert [l.source for l in cfg.layers] == ["defaults", "user"]


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
    assert [l.source for l in cfg.layers] == ["defaults", "user", "project"]


def test_repo_none_skips_project_layer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_toml(home / ".ortusrc", 'owner = "user-owner"\n')
    cfg = load_config(repo=None, home=home)
    assert cfg.values["owner"] == "user-owner"
    assert [l.source for l in cfg.layers] == ["defaults", "user"]


def test_config_get_returns_default_for_missing_key(tmp_path: Path) -> None:
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert cfg.get("nope", "fallback") == "fallback"
    assert cfg.get("owner") is None


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


def test_home_defaults_to_path_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When home=None, defaults to Path.home() — exercise the default branch."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    cfg = load_config(repo=None)
    assert [l.source for l in cfg.layers] == ["defaults"]
