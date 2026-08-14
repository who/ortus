"""Integration tests for --worker-timeout: per-iteration worker watchdog (ortus-w2ib).

A worker subprocess that is stuck-but-alive used to hang the entire grind
loop indefinitely — post-exit judging and idle-sleep only run AFTER the worker
exits, so a hung worker meant a human had to kill it by hand. --worker-timeout
hard-caps the iteration: the orchestrator SIGTERM/SIGKILLs the worker's whole
process group on exceed, logs the kill distinctly, then judges observable bd
status exactly like a clean exit (closed is a win; a claim left in_progress
stays claimed for the next window; open is no-change).

Each test installs a fake claude that hangs (sleeps far longer than the small
--worker-timeout) and confirms grind kills it, logs the TIMEOUT, recovers from
observable bd state, and exits hands-off. The distinct "TIMEOUT" log line is
the discriminating signal: it is written ONLY on the watchdog path, never on a
clean exit, so a regression where the worker is allowed to run to natural
completion cannot satisfy these assertions.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.sandbox import SandboxInfo
from tests._shims import make_inline_python_shim
from tests.conftest import copy_bd_workspace


# These tests spawn a real subprocess that hangs until the watchdog kills it;
# wall-clock is dominated by --worker-timeout (2s) plus the runner's
# SIGTERM→wait→SIGKILL grace (~a few secs), so mark them slow.
pytestmark = [pytest.mark.integration, pytest.mark.slow]
runner = CliRunner()


def _stub_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _seed_repo(tmp_path: Path) -> tuple[Path, str]:
    """Returns (repo, issue_id) — one ready issue.

    A ~25ms copy of the session's `leaf` template, which already carries the
    ready issue and the enabled .claude, rather than a `bd init` plus a
    `bd create` at roughly a second each (ortus-apmf).
    """
    workspace = copy_bd_workspace(tmp_path / "worker-timeout", "leaf")
    repo = workspace.path
    (repo / ".gitignore").write_text(
        "logs/\n.cache/\n.beads/ortus.flock\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, workspace.issues[0]


def _install_shim(monkeypatch: pytest.MonkeyPatch, shim: Path) -> None:
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )


def _force_fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


def _bd_show(repo: Path, issue_id: str) -> dict:
    proc = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout)
    if isinstance(data, list):
        return data[0]
    return data


def _grind_log(repo: Path) -> str:
    return sorted((repo / "logs").glob("grind-*.log"))[-1].read_text(encoding="utf-8")


# A worker that NEVER touches bd and sleeps far past the watchdog timeout.
_SLEEP_FOREVER = (
    'import time\nprint("hanging, no bd touch", flush=True)\ntime.sleep(120)\n'
)

# A worker that CLAIMS the first ready issue, then hangs (case 1: stuck-alive).
_CLAIM_THEN_HANG = textwrap.dedent(
    """\
    import json, subprocess, time
    ready = json.loads(subprocess.run(
        ["bd", "ready", "--json"], check=True, capture_output=True, text=True
    ).stdout)
    first = next((i["id"] for i in ready if i.get("issue_type") != "epic"), None)
    if first:
        subprocess.run(
            ["bd", "update", first, "--status", "in_progress"],
            check=True, stdout=subprocess.DEVNULL,
        )
        print(f"claimed {first}, now hanging", flush=True)
    time.sleep(120)
    """
)

# A worker that CLAIMS and CLOSES its issue, then hangs (case 2:
# hung-after-close). Under the on-main contract the worker owns the claim,
# so the shim walks the same path goal-prompt prescribes: `bd ready`, claim,
# close, and only then wedges.
_CLOSE_THEN_HANG = textwrap.dedent(
    """\
    import json, subprocess, time
    ready = json.loads(subprocess.run(
        ["bd", "ready", "--json"], check=True, capture_output=True, text=True
    ).stdout)
    first = next((i["id"] for i in ready if i.get("issue_type") != "epic"), None)
    if first:
        subprocess.run(
            ["bd", "update", first, "--status", "in_progress"],
            check=True, stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["bd", "close", first, "--reason", "shipped before hanging"],
            check=True, stdout=subprocess.DEVNULL,
        )
        print(f"closed {first}, now hanging", flush=True)
    time.sleep(120)
    """
)


def test_worker_timeout_kills_hung_worker_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that hangs without touching bd is killed within --worker-timeout;
    grind logs the TIMEOUT distinctly and exits hands-off (no-change branch)."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(
        monkeypatch, make_inline_python_shim(tmp_path, "claude-hang", _SLEEP_FOREVER)
    )

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--worker-timeout",
            "2",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _grind_log(repo)
    assert "worker TIMEOUT after 2s" in log, (
        f"watchdog should log the kill; got:\n{log}"
    )
    # The worker never claimed anything, so bd state is unchanged.
    assert _bd_show(repo, issue_id)["status"] == "open"


def test_worker_timeout_leaves_claim_for_next_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1 (stuck-alive): worker claims an issue then hangs. The watchdog
    kills it, and the claim stays in_progress for the next context window —
    bd status is ground truth, and a live claim is never reverted."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(
        monkeypatch,
        make_inline_python_shim(tmp_path, "claude-claim-hang", _CLAIM_THEN_HANG),
    )

    # Headroom: the worker runs two bd calls (ready/update) against dolt
    # before it starts hanging; 5s comfortably clears that latency while the
    # hang (sleep 120) still trips the watchdog.
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--worker-timeout",
            "5",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _bd_show(repo, issue_id)["status"] == "in_progress", (
        "killed worker's claim must stay claimed for the next window"
    )
    log = _grind_log(repo)
    assert "worker TIMEOUT after 5s" in log
    assert f"left {issue_id} in_progress for the next window" in log


def test_worker_timeout_counts_close_when_worker_hangs_after_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 2 (hung-after-close): worker closes its issue then hangs. The
    watchdog kills it, and because bd state is ground truth the close still
    counts — grind does not re-treat it as an orphan."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(
        monkeypatch,
        make_inline_python_shim(tmp_path, "claude-close-hang", _CLOSE_THEN_HANG),
    )

    # Headroom: the worker runs three bd calls (ready/update/close) against
    # dolt before it starts hanging, so the timeout must comfortably exceed
    # that latency — otherwise the watchdog kills it mid-close and the close
    # never lands. 60s clears it; the worker still hangs (sleep 120) well
    # past it.
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--worker-timeout",
            "60",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _bd_show(repo, issue_id)["status"] == "closed", (
        "a close that landed before the hang must survive the watchdog kill"
    )
    log = _grind_log(repo)
    assert "worker TIMEOUT after 60s" in log
    assert f"worker closed {issue_id}" in log


def test_worker_timeout_zero_disables_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--worker-timeout 0 passes timeout=None to the runner (opt-out). We
    verify the opt-out wiring without spawning a hanging worker: a no-op shim
    that exits immediately runs to completion and no TIMEOUT line appears."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(
        monkeypatch,
        make_inline_python_shim(
            tmp_path, "claude-noop", 'print("did nothing", flush=True)\n'
        ),
    )

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--worker-timeout",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "TIMEOUT" not in _grind_log(repo)
