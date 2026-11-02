"""An inherited BEADS_DIR steers bd only for the repo it belongs to.

`ortus grind` exports BEADS_DIR so worker clones share the primary tracker.
An Ortus command a worker aims at an unrelated scratch repo must not follow
that pin into the primary database.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ortus.core.bd as bd_mod
from ortus.cli import app
from ortus.core.bd import BEADS_DIR_ENV, scoped_bd_env

pytestmark = pytest.mark.integration
runner = CliRunner()

NOTICE = "ignoring inherited BEADS_DIR="


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _bd(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != BEADS_DIR_ENV}
    return subprocess.run(
        ["bd", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def fresh_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bd_mod, "_foreign_beads_dir_noticed", False)


@pytest.fixture()
def primary(tmp_path: Path) -> Path:
    repo = _git_repo(tmp_path / "primary")
    (repo / ".beads").mkdir()
    return repo


# AC-1


def test_scratch_init_gets_its_own_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ortus.commands.init as init_mod

    primary = _git_repo(tmp_path / "primary")
    _bd(primary, "init", "--non-interactive", "--prefix", "prim")
    _bd(primary, "create", "--title", "primary work", "--type", "task")
    before_issues = json.loads(_bd(primary, "list", "--all", "--json"))
    before_memories = _bd(primary, "memories", "--json")

    scratch = _git_repo(tmp_path / "scratch")
    monkeypatch.setenv(BEADS_DIR_ENV, str(primary / ".beads"))
    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: "/usr/bin/codegraph")
    monkeypatch.setattr(
        init_mod,
        "_codegraph_index",
        lambda repo, **kw: (repo / ".codegraph").mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")

    result = runner.invoke(
        app, ["init", str(scratch), "--stealth", "--prefix", "scr"]
    )
    out = (result.stdout or "") + (result.stderr or "")
    assert result.exit_code == 0, out
    assert (scratch / ".beads").is_dir()
    assert NOTICE in out

    monkeypatch.delenv(BEADS_DIR_ENV)
    assert json.loads(_bd(primary, "list", "--all", "--json")) == before_issues
    assert _bd(primary, "memories", "--json") == before_memories


# AC-2


def test_clone_keeps_inherited_beads_dir(
    primary: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = tmp_path / "workspace" / "clone"
    clone.parent.mkdir()
    _git(clone.parent, "clone", "-q", str(primary), str(clone))
    beads_dir = str(primary / ".beads")

    env = scoped_bd_env(clone, {BEADS_DIR_ENV: beads_dir, "PATH": "/bin"})
    assert env[BEADS_DIR_ENV] == beads_dir
    # The primary itself, and a subdirectory of it, keep the pin too.
    assert scoped_bd_env(primary, {BEADS_DIR_ENV: beads_dir})[BEADS_DIR_ENV] == beads_dir
    sub = primary / "sub"
    sub.mkdir()
    assert scoped_bd_env(sub, {BEADS_DIR_ENV: beads_dir})[BEADS_DIR_ENV] == beads_dir
    assert NOTICE not in capsys.readouterr().err


def test_clone_keeps_unset_is_noop(primary: Path) -> None:
    assert scoped_bd_env(primary, {"PATH": "/bin"}) == {"PATH": "/bin"}


# AC-3


def test_foreign_notice_printed_once(
    primary: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = _git_repo(tmp_path / "scratch")
    beads_dir = str(primary / ".beads")
    inherited = {BEADS_DIR_ENV: beads_dir, "PATH": "/bin"}

    first = scoped_bd_env(scratch, inherited)
    second = scoped_bd_env(scratch, inherited)

    assert BEADS_DIR_ENV not in first and BEADS_DIR_ENV not in second
    assert inherited[BEADS_DIR_ENV] == beads_dir  # caller's mapping untouched
    err = capsys.readouterr().err
    assert err.count(NOTICE) == 1
    assert f"{NOTICE}{beads_dir}; it belongs to another repo" in err


def test_notice_for_missing_beads_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = _git_repo(tmp_path / "scratch")
    missing = str(tmp_path / "gone" / ".beads")
    assert BEADS_DIR_ENV not in scoped_bd_env(scratch, {BEADS_DIR_ENV: missing})
    assert capsys.readouterr().err.count(NOTICE) == 1
