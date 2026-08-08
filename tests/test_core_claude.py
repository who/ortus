"""Tests for core/claude.py — claude subprocess wrapper (xvel.1 acceptance)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ortus.core.claude import (
    STANDARD_FLAGS,
    ClaudeRunner,
    _kill_group,
    _readonly_wrapper,
    _repo_source_readonly,
)
from ortus.core.profiles import AgentProfile, Phase
from tests._platform import (
    skip_unless_bwrap_usable,
    skip_unless_tmp_is_canonical,
)
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
def test_linux_readonly_wrapper_keeps_a_repo_under_tmp_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo under /tmp survives `--tmpfs /tmp` and keeps the inverted posture.

    `--tmpfs /tmp` wipes the mount, so the repo has to be restored after it or
    the verifier chdirs into an empty directory. The repo mounts then have to
    land after *that*, or the restore masks them (ortus-dyio, ortus-v8fn).
    """
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    repo = Path("/tmp/ortus-readonly-wrapper/nested/repo")

    argv = _readonly_wrapper(["claude", "-p", "verify"], repo)

    resolved = str(repo.resolve())
    assert argv[:5] == ["bwrap", "--ro-bind", "/", "/", "--dev-bind"]
    assert ["--tmpfs", "/tmp"] == argv[
        argv.index("--tmpfs") : argv.index("--tmpfs") + 2
    ]
    assert argv[argv.index("--chdir") + 1] == resolved


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


def test_readonly_wrapper_binds_the_repo_writable_and_re_binds_source_readonly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-v8fn: tool state under the repo is writable, source is not."""
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    repo = tmp_path / "repo"
    for name in ("src", "tests", ".git", ".claude"):
        (repo / name).mkdir(parents=True)
    (repo / "README.md").write_text("readme", encoding="utf-8")

    argv = _readonly_wrapper(["claude", "-p", "verify"], repo)

    resolved = repo.resolve()
    # The repo itself is bound writable...
    assert any(
        tok == "--bind" and argv[i + 1 : i + 3] == [str(resolved), str(resolved)]
        for i, tok in enumerate(argv)
    ), f"repo not bound writable in {argv}"
    # ...and every non-tool entry is re-bound read-only on top of it.
    readonly = {
        argv[i + 1] for i, tok in enumerate(argv) if tok == "--ro-bind"
    }
    for name in ("src", "tests", "README.md"):
        assert str(resolved / name) in readonly, f"{name} must stay read-only"
    # Tool state is deliberately left writable: git lock files, the agent CLI's
    # deny-rule placeholders and its project config all get written by an
    # ordinary review, and none of them are code under test.
    for name in (".git", ".claude"):
        assert str(resolved / name) not in readonly, f"{name} must stay writable"


def test_readonly_wrapper_tolerates_a_repo_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bwrap refuses to launch on a missing bind source, so emit no repo mounts."""
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    assert _repo_source_readonly(tmp_path / "absent") == []


@skip_unless_bwrap_usable
def test_readonly_wrapper_keeps_repo_claude_writable_under_tmp(
    tmp_path: Path,
) -> None:
    """ortus-dyio: the agent-dir tmpfs has to outrank the /tmp repo re-bind.

    A repo under /tmp is wiped by `--tmpfs /tmp` and restored by a read-only
    bind. bwrap applies mounts in order, so a `.claude` tmpfs staged before that
    bind is masked by it and the inner sandbox is back to the read-only
    `<repo>/.claude` that stopped verifiers dead. Executed rather than asserted
    on argv, because the argv carried both mounts while the posture did not.
    """
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        pytest.skip("bubblewrap posture required")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".claude").mkdir()

    argv = _readonly_wrapper(
        [
            "/bin/sh",
            "-c",
            "mkdir -p .claude/hooks && echo PLACEHOLDER_OK; "
            "touch src/mutated 2>/dev/null || echo CANDIDATE_READONLY",
        ],
        repo,
    )
    proc = subprocess.run(argv, capture_output=True, text=True)

    combined = proc.stdout + proc.stderr
    assert "PLACEHOLDER_OK" in combined, combined
    assert "CANDIDATE_READONLY" in combined, combined
    # The placeholder persists: under the inverted posture (ortus-v8fn) tool
    # state is a real bind, not a discarded tmpfs, because a lock file the
    # sandbox cannot see is a lock file git cannot take. Mutation of the
    # candidate is caught by the post-verdict hashes, not by the mount.
    assert (repo / ".claude" / "hooks").exists()


def test_readonly_wrapper_skips_repo_claude_dir_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ortus.core.claude.platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()

    argv = _readonly_wrapper(["claude", "-p", "verify"], repo)

    assert str((repo / ".claude").resolve()) not in argv


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
