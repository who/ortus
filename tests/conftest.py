"""Shared test fixtures.

Per FR-034 (Testing Strategy): claude_mock fixture loads a canned scenario
by key. Scenarios live in tests/fixtures/canned-claude-responses/<key>.sh
and behave as drop-in replacements for the real claude binary.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

_FIXTURES = Path(__file__).parent / "fixtures"
_CANNED_DIR = _FIXTURES / "canned-claude-responses"


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
        path = _CANNED_DIR / f"{scenario}.sh"
        if not path.is_file():
            available = sorted(p.stem for p in _CANNED_DIR.glob("*.sh"))
            raise FileNotFoundError(
                f"no canned claude scenario named {scenario!r}; available: {available}"
            )
        return path

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
