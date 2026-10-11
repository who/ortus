"""Jev is gone, and a `.ortusrc` that still pins it loads with one warning."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import ortus.core.config as config_mod
from ortus.core.config import load_config

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]

PLAIN = 'backend = "claude"\nreviewer = true\n'
LEGACY_TABLE = """
[judge]
enabled = true
mode = "enforce"
pre_tool = true

[judge.seats.x]
model = "some-model"
"""
LEGACY_KEYS = 'jev_model_router = true\njev_progress_reaper = "enforce"\n'


@pytest.fixture(autouse=True)
def fresh_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test is its own process as far as the once-guard is concerned."""
    monkeypatch.setattr(config_mod, "_legacy_judge_warned", False)
    for name in list(os.environ):
        if name.startswith(config_mod.LEGACY_ENV_PREFIXES):
            monkeypatch.delenv(name)


def _repo(tmp_path: Path, text: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".ortusrc").write_text(text, encoding="utf-8")
    return repo


def _home(tmp_path: Path, text: str | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if text is not None:
        (home / ".ortusrc").write_text(text, encoding="utf-8")
    return home


def _warnings(err: str) -> list[str]:
    """Each warning as one string; Rich may wrap a long one across lines."""
    text = " ".join(err.split())
    return ["warn:" + part for part in text.split("warn:")[1:] if "legacy Jev config" in part]


def test_no_judge_modules() -> None:
    assert sorted((REPO_ROOT / "src" / "ortus" / "core").glob("judge*.py")) == []
    probe = (
        "import sys, ortus.cli; "
        "print([m for m in sys.modules if 'judge' in m])"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_legacy_table_and_keys_load_like_the_plain_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _home(tmp_path)
    plain = load_config(repo=_repo(tmp_path / "plain", PLAIN), home=home)
    assert _warnings(capsys.readouterr().err) == []

    repo = _repo(tmp_path, PLAIN + LEGACY_KEYS + LEGACY_TABLE)
    cfg = load_config(repo=repo, home=home)
    captured = capsys.readouterr()

    assert cfg.values == plain.values
    assert "judge" not in cfg.layers[-1].data
    assert captured.out == ""
    [line] = _warnings(captured.err)
    assert str(repo / ".ortusrc") in line
    for key in ("[judge]", "jev_model_router", "jev_progress_reaper"):
        assert key in line


@pytest.mark.parametrize("table", ["[judge]\n", "judge = 1\n"])
def test_legacy_table_empty_or_scalar_is_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], table: str
) -> None:
    cfg = load_config(repo=_repo(tmp_path, table), home=_home(tmp_path))
    assert "judge" not in cfg.values
    assert len(_warnings(capsys.readouterr().err)) == 1


def test_legacy_keys_in_both_layers_warn_once_naming_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _home(tmp_path, LEGACY_KEYS)
    repo = _repo(tmp_path, LEGACY_TABLE)
    load_config(repo=repo, home=home)
    load_config(repo=repo, home=home)
    [line] = _warnings(capsys.readouterr().err)
    assert str(home / ".ortusrc") in line
    assert str(repo / ".ortusrc") in line


def test_legacy_keys_only_in_user_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _home(tmp_path, LEGACY_KEYS)
    cfg = load_config(repo=_repo(tmp_path, PLAIN), home=home)
    assert not any(key.startswith("jev_") for key in cfg.values)
    [line] = _warnings(capsys.readouterr().err)
    assert str(home / ".ortusrc") in line


def test_legacy_env_is_ignored_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _home(tmp_path)
    repo = _repo(tmp_path, PLAIN)
    plain = load_config(repo=repo, home=home)
    capsys.readouterr()

    monkeypatch.setenv("ORTUS_JUDGE_ENABLED", "1")
    monkeypatch.setenv("ORTUS_JEV_ROUTER", "1")
    cfg = load_config(repo=repo, home=home)
    captured = capsys.readouterr()

    assert cfg.values == plain.values
    assert captured.out == ""
    [line] = _warnings(captured.err)
    assert "ORTUS_JUDGE_ENABLED" in line
    assert "ORTUS_JEV_ROUTER" in line


def test_no_warning_without_legacy_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    load_config(repo=_repo(tmp_path, PLAIN), home=_home(tmp_path, PLAIN))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_extra_removed() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "typesafe-sdk" not in text
    project = tomllib.loads(text)["project"]
    assert "judge" not in project.get("optional-dependencies", {})


def test_own_ortusrc_has_no_judge_table() -> None:
    data = tomllib.loads((REPO_ROOT / ".ortusrc").read_text(encoding="utf-8"))
    assert "judge" not in data
    assert not any(key.startswith("jev_") for key in data)
