"""`ortus init --stealth` end to end on a fork-like repository.

Real git and the real bd binary, no seams stubbed except CodeGraph (pinned
off on the command line): the invariants are about what bd and git actually
leave behind. The fork is a clone of a bare `upstream.git` whose `origin`
points at a second bare repository standing in for the operator's GitHub
fork, so every push lands somewhere a test can read back with `ls-remote`.
HOME and XDG_CONFIG_HOME point into the test's tmp dir, so neither init nor
bd can touch the real global git config.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.grind import _announced_push
from ortus.core.agent_files import read_block
from ortus.core.git import GitClient
from ortus.core.stealth import exclude_path, tracked_ortus_paths

pytestmark = [pytest.mark.integration, pytest.mark.real_bd, pytest.mark.slow]
runner = CliRunner()

UPSTREAM_GITIGNORE = b"node_modules/\n*.pyc\n"
UPSTREAM_FILES = {
    ".gitignore": UPSTREAM_GITIGNORE.decode(),
    "README.md": "upstream project\n",
    "src/app.py": "VALUE = 1\n",
}
EXCLUDE_BEGIN = "# BEGIN ortus stealth v1"


@pytest.fixture(autouse=True)
def _require_bd() -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd binary not on PATH")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    ).stdout.strip()


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@dataclass
class Fork:
    repo: Path
    origin: Path
    home: Path

    def origin_refs(self) -> dict[str, str]:
        out = _git(self.repo, "ls-remote", str(self.origin))
        return {ref: sha for sha, ref in (line.split("\t") for line in out.splitlines())}

    def home_config(self) -> dict[str, bytes | None]:
        files = [self.home / ".gitconfig", self.home / ".config" / "git" / "ignore"]
        return {str(f): f.read_bytes() if f.is_file() else None for f in files}


def _make_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, str] | None = None
) -> Fork:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # The hooks a stealth init installs exec `ortus`, so the interpreter's own
    # bin directory has to be on PATH for a push to reach the guard.
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("BEADS_DIR", raising=False)
    monkeypatch.delenv("BD_NO_REMOTE_ADOPT", raising=False)
    _git(tmp_path, "config", "--global", "user.email", "t@example.com")
    _git(tmp_path, "config", "--global", "user.name", "t")

    upstream = tmp_path / "upstream.git"
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(upstream), str(seed))
    _write(seed, {**UPSTREAM_FILES, **(extra or {})})
    _commit_all(seed, "Upstream history")
    _git(seed, "push", "-q", "origin", "main")

    repo = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(upstream), str(repo))
    _git(repo, "remote", "rename", "origin", "upstream")
    _git(repo, "remote", "add", "origin", str(origin))
    # The origin stand-in starts with main pushed, as a GitHub fork does.
    _git(repo, "push", "-q", "-u", "origin", "main")
    return Fork(repo=repo, origin=origin, home=home)


@pytest.fixture()
def fork(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fork:
    return _make_fork(tmp_path, monkeypatch)


def _init(repo: Path, *extra: str) -> None:
    result = runner.invoke(
        app,
        ["init", str(repo), "--stealth", "--backend", "claude", "--codegraph", "off", *extra],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")


def _exclude_text(repo: Path) -> str:
    return exclude_path(repo).read_text(encoding="utf-8")


def _push(repo: Path) -> bool:
    return _announced_push(GitClient(repo, stealth=True), "main")


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain", "--untracked-files=all")


def test_init_invariants_on_a_fork(fork: Fork) -> None:
    repo = fork.repo
    head = _git(repo, "rev-parse", "HEAD")
    count = _git(repo, "rev-list", "--count", "HEAD")
    tracked = _git(repo, "ls-files")
    home_before = fork.home_config()
    refs_before = fork.origin_refs()

    _init(repo)

    assert tracked_ortus_paths(repo) == ()
    assert _git(repo, "ls-files") == tracked
    assert (repo / ".gitignore").read_bytes() == UPSTREAM_GITIGNORE
    assert _git(repo, "config", "--get", "core.hooksPath", check=False) == ""
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "rev-list", "--count", "HEAD") == count
    assert _status(repo) == ""
    assert _exclude_text(repo).count(EXCLUDE_BEGIN) == 1
    assert _git(repo, "config", "--local", "--get", "ortus.stealth") == "true"
    no_git_ops = subprocess.run(
        ["bd", "config", "get", "no-git-ops"], cwd=repo, capture_output=True, text=True
    )
    assert no_git_ops.stdout.strip().splitlines()[-1] == "true", no_git_ops.stdout
    assert fork.home_config() == home_before
    assert fork.origin_refs() == refs_before
    # The files Ortus wrote are on disk, just invisible to git.
    assert (repo / ".ortusrc").is_file() and (repo / ".beads").is_dir()


def test_origin_refs_stay_free_of_tracker_data(fork: Fork) -> None:
    repo = fork.repo
    _init(repo)

    _write(repo, {"src/app.py": "VALUE = 2\n"})
    _commit_all(repo, "Raise the app constant")
    assert _push(repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert fork.origin_refs() == {"HEAD": head, "refs/heads/main": head}

    subprocess.run(
        ["bd", "dolt", "push"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "BD_NO_REMOTE_ADOPT": "1"},
    )
    # Even a hand-typed push of tracker refs to origin is refused by the hook.
    leak = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/dolt/data"], cwd=repo, capture_output=True
    )
    assert leak.returncode != 0

    refs = fork.origin_refs()
    assert not any(ref.startswith("refs/dolt/") for ref in refs), refs
    assert "refs/heads/__dolt_remote_info__" not in refs
    assert refs == {"HEAD": head, "refs/heads/main": head}


def test_guard_blocks_force_added_ortus_path(fork: Fork) -> None:
    repo = fork.repo
    _init(repo)
    before = fork.origin_refs()

    _git(repo, "add", "-f", ".beads/config.yaml")
    _git(repo, "commit", "-q", "-m", "Add tracker config")
    assert not _push(repo)
    assert subprocess.run(
        ["git", "push", "origin", "main"], cwd=repo, capture_output=True
    ).returncode != 0
    assert fork.origin_refs() == before


def test_guard_blocks_bead_id_subject(fork: Fork) -> None:
    repo = fork.repo
    _init(repo)
    before = fork.origin_refs()

    _write(repo, {"README.md": "upstream project, patched\n"})
    _commit_all(repo, "fork-abc: patch the readme")
    assert not _push(repo)
    assert subprocess.run(
        ["git", "push", "origin", "main"], cwd=repo, capture_output=True
    ).returncode != 0
    assert fork.origin_refs() == before

    # The same change under a plain subject goes through.
    _git(repo, "commit", "-q", "--amend", "-m", "Patch the readme")
    assert _push(repo)
    assert fork.origin_refs()["refs/heads/main"] == _git(repo, "rev-parse", "HEAD")


def test_agents_tracked_upstream_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_agents = "# Upstream agent notes\n"
    fork = _make_fork(tmp_path, monkeypatch, {"AGENTS.md": upstream_agents})
    repo = fork.repo
    _init(repo)

    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == upstream_agents
    assert read_block(repo / "AGENTS.override.md", "agents") is not None
    assert _git(repo, "check-ignore", "AGENTS.override.md") == "AGENTS.override.md"
    assert "/AGENTS.md" not in _exclude_text(repo).splitlines()
    assert _status(repo) == ""


def test_force_rerun_is_idempotent(fork: Fork) -> None:
    repo = fork.repo
    _init(repo)
    exclude = _exclude_text(repo)
    head = _git(repo, "rev-parse", "HEAD")

    _init(repo, "--force")

    assert _exclude_text(repo) == exclude
    assert _exclude_text(repo).count(EXCLUDE_BEGIN) == 1
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".gitignore").read_bytes() == UPSTREAM_GITIGNORE
    assert _status(repo) == ""
