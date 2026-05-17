"""Tests for core/claude.py — claude subprocess wrapper (xvel.1 acceptance)."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from ortus.core.claude import STANDARD_FLAGS, ClaudeRunner, _kill_group
from tests._shims import shim_path

FAKE_CLAUDE = shim_path("fake-claude")


# --- argv assembly ----------------------------------------------------------


def test_standard_flags_present_and_in_order() -> None:
    """Acceptance #1: argv contains the 4 standard flags."""
    runner = ClaudeRunner()
    argv = runner.build_argv("do thing")
    assert argv[0] == "claude"
    assert argv[1:3] == ["-p", "do thing"]
    for flag in STANDARD_FLAGS:
        assert flag in argv
    assert "--fast" not in argv


def test_fast_flag_added_when_requested() -> None:
    """Acceptance #4: --fast absent by default, present exactly once when fast=True."""
    runner = ClaudeRunner()
    argv = runner.build_argv("do thing", fast=True)
    assert argv.count("--fast") == 1


def test_fast_false_omits_flag() -> None:
    runner = ClaudeRunner()
    argv = runner.build_argv("do thing", fast=False)
    assert "--fast" not in argv


# --- tee-to-log-not-terminal -----------------------------------------------


def test_output_tees_to_log_not_terminal(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Acceptance #2: parent terminal stdout/stderr is empty; log_path gets bytes."""
    assert FAKE_CLAUDE.exists(), "shim missing — fix tests/fixtures/bin/fake-claude.py"
    log = tmp_path / "logs" / "grind.log"
    runner = ClaudeRunner(claude_binary=str(FAKE_CLAUDE))
    rc = runner.run("hello", repo=tmp_path, log_path=log)
    assert rc == 0
    out, err = capfd.readouterr()
    assert out == "", f"parent stdout should be empty, got: {out!r}"
    assert err == "", f"parent stderr should be empty, got: {err!r}"
    log_text = log.read_text()
    assert "fake-claude argv:" in log_text
    assert "fake-claude done" in log_text


# --- signal handling --------------------------------------------------------


def test_sigint_terminates_child_within_two_seconds(
    tmp_path: Path,
) -> None:
    """Acceptance #3: SIGINT to parent terminates child within 2s."""
    log = tmp_path / "log.txt"

    # Launch the fake claude in a sleep loop; assert _kill_group reaps it fast.
    env = {**os.environ, "FAKE_CLAUDE_SLEEP": "30"}
    proc = subprocess.Popen(
        [str(FAKE_CLAUDE)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=open(log, "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # Give it a moment to start the sleep.
    time.sleep(0.2)
    assert proc.poll() is None, "fake-claude should still be running"

    t0 = time.monotonic()
    _kill_group(proc)
    elapsed = time.monotonic() - t0
    assert proc.poll() is not None, "child should be dead after _kill_group"
    assert elapsed < 2.0, f"reap took {elapsed:.2f}s (must be < 2s)"


def test_kill_group_safe_when_proc_already_dead(tmp_path: Path) -> None:
    """_kill_group must be no-op on an already-exited process."""
    proc = subprocess.Popen(
        [str(FAKE_CLAUDE)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc.wait()
    assert proc.poll() == 0
    # Should not raise.
    _kill_group(proc)


def test_exit_code_propagates(tmp_path: Path) -> None:
    runner = ClaudeRunner(
        claude_binary=str(FAKE_CLAUDE),
        extra_env={"FAKE_CLAUDE_EXIT": "7"},
    )
    log = tmp_path / "log.txt"
    rc = runner.run("hello", repo=tmp_path, log_path=log)
    assert rc == 7


def test_timeout_kills_child(tmp_path: Path) -> None:
    runner = ClaudeRunner(
        claude_binary=str(FAKE_CLAUDE),
        extra_env={"FAKE_CLAUDE_SLEEP": "30"},
    )
    log = tmp_path / "log.txt"
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run("hello", repo=tmp_path, log_path=log, timeout=0.5)
