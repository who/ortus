"""Tests for core/claude.py — claude subprocess wrapper (xvel.1 acceptance)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ortus.core.claude import STANDARD_FLAGS, ClaudeRunner, _kill_group, _readonly_wrapper
from ortus.core.profiles import AgentProfile, Phase
from tests._platform import skip_unless_tmp_is_canonical
from tests._shims import shim_path

FAKE_CLAUDE = shim_path("fake-claude")

# start_new_session is POSIX-only. On Windows the kwarg is ignored at the C
# layer but we omit it for clarity (and to keep the cross-platform Popen
# invocation patterns consistent with core/claude.py).
_NEW_SESSION_KWARGS: dict = (
    {} if sys.platform == "win32" else {"start_new_session": True}
)


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


def test_profile_routes_model_and_effort() -> None:
    profile = AgentProfile("claude", Phase.PLAN, "opus", "high")
    argv = ClaudeRunner().build_argv("do thing", profile=profile)
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"


def test_unset_profile_preserves_old_argv() -> None:
    plain = ClaudeRunner().build_argv("do thing")
    unset = ClaudeRunner().build_argv(
        "do thing", profile=AgentProfile("claude", Phase.VERIFY)
    )
    assert unset == plain


def test_readonly_argv_denies_provider_write_tools() -> None:
    argv = ClaudeRunner().build_argv("verify", readonly=True)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    denied = argv[argv.index("--disallowedTools") + 1]
    assert all(tool in denied for tool in ("Write", "Edit", "NotebookEdit"))


@skip_unless_tmp_is_canonical
def test_linux_readonly_wrapper_keeps_repo_under_tmp_visible_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    # The repo must be under a canonical /tmp or the wrapper's
    # `relative_to("/tmp")` guard fails and no --ro-bind pair is emitted at
    # all. `tmp_path` does not guarantee that — on macOS it lands in
    # /var/folders — so name the path directly. The wrapper only reads it,
    # never touches the filesystem, so it need not exist.
    repo = Path("/tmp/ortus-readonly-wrapper/nested/repo")
    resolved = str(repo.resolve())
    assert resolved.startswith("/tmp/"), resolved

    argv = _readonly_wrapper(["claude", "-p", "verify"], repo)

    assert argv[:5] == ["bwrap", "--ro-bind", "/", "/", "--dev-bind"]
    # Find the pair by its flag. Indexing on the bare path instead silently
    # matches the `--chdir` occurrence when the pair was never emitted, so a
    # skipped branch read as a wrong-order argv rather than a missing bind.
    assert any(
        token == "--ro-bind" and argv[i + 1 : i + 3] == [resolved, resolved]
        for i, token in enumerate(argv)
    ), f"no --ro-bind pair for {resolved} in {argv}"
    assert argv[argv.index("--chdir") + 1] == resolved
    assert ["--tmpfs", "/tmp"] == argv[
        argv.index("--tmpfs") : argv.index("--tmpfs") + 2
    ]


def test_readonly_wrapper_gives_agent_scratch_dirs_a_writable_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-dyio: `--ro-bind / /` leaves $HOME read-only, and the agent CLI
    cannot start a shell without writing its per-session dirs."""
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    home = tmp_path / "home"
    (home / ".claude" / "session-env").mkdir(parents=True)
    (home / ".claude" / "sessions").mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    argv = _readonly_wrapper(["claude", "-p", "verify"], tmp_path / "repo")

    mounted = {argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"}
    assert str(home / ".claude" / "session-env") in mounted
    assert str(home / ".claude" / "sessions") in mounted
    # Absent dirs are skipped: the root is bound read-only, so bwrap cannot
    # create a missing mountpoint and would fail to launch.
    assert str(home / ".claude" / "projects") not in mounted
    # The read-only posture that protects the candidate is unchanged.
    assert argv[:4] == ["bwrap", "--ro-bind", "/", "/"]


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
    log_text = log.read_text(encoding="utf-8")
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
        **_NEW_SESSION_KWARGS,
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
        **_NEW_SESSION_KWARGS,
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
