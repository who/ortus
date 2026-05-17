"""Shared test fixtures.

Per FR-034 (Testing Strategy): claude_mock fixture loads a canned scenario
by key. Scenarios live in tests/fixtures/canned-claude-responses/<key>.py
and behave as drop-in replacements for the real claude binary. Resolution
goes through tests._shims.shim_path so each scenario returns a path the
host OS can execute directly (POSIX: the .py file with +x; Windows: a
generated .bat wrapper). See ortus-f4bu.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from tests._shims import shim_path

_FIXTURES = Path(__file__).parent / "fixtures"
_CANNED_DIR = _FIXTURES / "canned-claude-responses"

# Auth files the real claude binary reads from ~/.claude/. The hermetic
# fake-HOME used by smoke tests would otherwise hide these and make claude
# exit with "Not logged in" (ortus-v0uw). We allow-list the minimum set —
# do NOT mirror the whole directory, since hermeticity for memory paths,
# settings, sessions, etc. is the whole point of fake HOME.
_CLAUDE_AUTH_FILES = (
    ".credentials.json",   # current claude (>=1.x): hidden, 0600
    "credentials.json",    # older/forward-compat variant
    "auth.json",           # older/forward-compat variant
)


def _link_claude_auth(real_home: Path, fake_home: Path) -> None:
    """Symlink the operator's claude auth files into a fake HOME.

    Pierces hermeticity for auth files only. Bd state, claude memory paths,
    settings overrides, and session state continue to resolve under
    `fake_home` so each test still gets a clean slate.

    No-op if the operator has no ~/.claude/ or no auth files (caller should
    then skip the test — see `claude_authenticated`).
    """
    real_claude = real_home / ".claude"
    if not real_claude.exists():
        return
    fake_claude = fake_home / ".claude"
    fake_claude.mkdir(parents=True, exist_ok=True)
    for name in _CLAUDE_AUTH_FILES:
        src = real_claude / name
        if not src.exists():
            continue
        dest = fake_claude / name
        if dest.exists() or dest.is_symlink():
            continue
        dest.symlink_to(src)


def claude_authenticated() -> bool:
    """Return True when claude auth is reachable on this host.

    Evaluated at collection time via `pytest.mark.skipif`, so it must not
    depend on monkeypatched env vars. We check the operator's real home
    directory for any of the allow-listed auth files. The override env var
    `ORTUS_SKIP_CLAUDE_AUTH_CHECK=1` forces the check to pass (for future
    CI runs with a mocked claude).
    """
    if os.environ.get("ORTUS_SKIP_CLAUDE_AUTH_CHECK") == "1":
        return True
    real_claude = Path.home() / ".claude"
    if not real_claude.exists():
        return False
    return any(
        (real_claude / name).is_file() and (real_claude / name).stat().st_size > 0
        for name in _CLAUDE_AUTH_FILES
    )


requires_claude_auth = pytest.mark.skipif(
    not claude_authenticated(),
    reason=(
        "claude auth not available; slow tests require operator login. "
        "Run `claude login` so ~/.claude/.credentials.json exists, or set "
        "ORTUS_SKIP_CLAUDE_AUTH_CHECK=1 to override (e.g. when claude is mocked)."
    ),
)


@pytest.fixture()
def claude_mock() -> Callable[[str], Path]:
    """Return a callable that resolves a canned scenario name → shim path.

    Usage:
        def test_x(claude_mock, monkeypatch):
            from ortus.commands import grind as gm
            from ortus.core.claude import ClaudeRunner
            shim = claude_mock("grind-empty-queue")
            monkeypatch.setattr(gm, "_make_runner",
                                lambda: ClaudeRunner(claude_binary=str(shim)))
    """

    def _resolve(scenario: str) -> Path:
        source = _CANNED_DIR / f"{scenario}.py"
        if not source.is_file():
            available = sorted(p.stem for p in _CANNED_DIR.glob("*.py"))
            raise FileNotFoundError(
                f"no canned claude scenario named {scenario!r}; available: {available}"
            )
        return shim_path(scenario)

    return _resolve


@pytest.fixture()
def seeded_3_issues(tmp_path: Path) -> Path:
    """A fresh bd workspace with 1 epic + 2 children (1 ready, 1 blocked).

    Returns the repo root.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "seeded"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "seed"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    # Epic
    epic = subprocess.run(
        ["bd", "create", "--silent", "--title", "Test epic", "--type", "epic", "--priority", "1"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Ready child
    ready = subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "Child ready", "--type", "task", "--priority", "2",
            "--parent", epic,
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Blocked child (depends on the ready one being closed first)
    blocked = subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "Child blocked", "--type", "task", "--priority", "2",
            "--parent", epic,
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Make `blocked` depend on `ready` (ready blocks blocked).
    subprocess.run(
        ["bd", "dep", "add", blocked, ready],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo
