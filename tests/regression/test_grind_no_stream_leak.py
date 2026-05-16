"""Regression: ortus grind must not leak claude stream-json to the parent terminal.

Catches the ortus-6q8v / goal.sh terminal-leak bug. If commands/grind.py
ever pipes claude's stdout to the parent, this test fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.regression

FAKE_CLAUDE_STREAM = (
    Path(__file__).parent.parent / "fixtures" / "bin" / "fake-claude-stream"
)


def _stream_json_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith('{"type":')]


def test_grind_terminal_is_quiet_log_is_full(tmp_path: Path) -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")

    # Build a self-contained fixture repo with .beads/ + .claude/settings.json.
    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "ttq"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

    # Drive the verb in a subprocess so stdout/stderr capture is faithful to
    # what a launching terminal sees — typer's CliRunner intercepts streams
    # and would obscure the leak. We use the test-time fake claude shim by
    # monkey-piping ortus through a tiny driver script.
    driver = tmp_path / "driver.py"
    driver.write_text(
        f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent.parent.parent / 'src')!r})
from ortus.core.claude import ClaudeRunner
from ortus.core import sandbox as _sb
from ortus.core.sandbox import SandboxInfo
from ortus.commands import grind as gm
from pathlib import Path

# Bypass platform sandbox check (CI runners may not have bwrap).
_sb.smoke_test = lambda: SandboxInfo(platform='Linux', binary='bwrap')
# Force home so user's real ~/.claude isn't read.
Path.home = classmethod(lambda cls: Path({str(tmp_path / 'fake-home')!r}))
# Swap in the fake-claude-stream shim.
gm._make_runner = lambda: ClaudeRunner(claude_binary={str(FAKE_CLAUDE_STREAM)!r})

from ortus.cli import app
app(['grind', {str(repo)!r}])
"""
    )
    proc = subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The driver script may exit non-zero from typer.Exit; we care about the
    # streams, not the rc.
    leaked = _stream_json_lines(proc.stdout)
    assert leaked == [], (
        f"REGRESSION: claude stream-json leaked to parent stdout:\n"
        f"  leaked lines: {leaked!r}\n"
        f"  full stdout: {proc.stdout!r}"
    )

    log_dir = repo / "logs"
    logs = list(log_dir.glob("grind-*.log"))
    assert logs, (
        f"expected a grind-*.log under {log_dir}\n"
        f"driver stdout: {proc.stdout!r}\n"
        f"driver stderr: {proc.stderr!r}\n"
        f"driver rc: {proc.returncode}"
    )
    log_text = logs[0].read_text()
    in_log = _stream_json_lines(log_text)
    assert in_log, "expected stream-json lines in the log file (not just the terminal)"
