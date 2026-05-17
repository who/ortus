"""Tests for core/repo.py — FR-003 resolve_repo (q075.3 acceptance #2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.repo import FR003_NO_BEADS_ERROR, resolve_repo


def test_resolve_repo_with_beads_returns_resolved_path(tmp_path: Path) -> None:
    repo = tmp_path / "ok"
    (repo / ".beads").mkdir(parents=True)
    out = resolve_repo(repo)
    assert out == repo.resolve()


def test_resolve_repo_missing_beads_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bogus = tmp_path / "no-beads"
    bogus.mkdir()
    import typer

    with pytest.raises(typer.Exit) as exc:
        resolve_repo(bogus)
    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert FR003_NO_BEADS_ERROR in captured.err


def test_resolve_repo_defaults_to_pwd_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pwd = tmp_path / "pwd-repo"
    (pwd / ".beads").mkdir(parents=True)
    monkeypatch.chdir(pwd)
    out = resolve_repo(None)
    assert out == pwd.resolve()


def test_resolve_repo_no_walk_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-003: explicit no walk-up to parents."""
    parent = tmp_path / "ancestor"
    (parent / ".beads").mkdir(parents=True)
    child = parent / "subdir"
    child.mkdir()
    monkeypatch.chdir(child)
    import typer

    with pytest.raises(typer.Exit) as exc:
        resolve_repo(None)
    assert exc.value.exit_code == 1
