"""Integration tests for --worker-timeout: per-iteration worker watchdog (ortus-w2ib).

A worker subprocess that is stuck-but-alive used to hang the entire grind
loop indefinitely — orphan-policy and idle-sleep only run AFTER the worker
exits, so a hung worker meant a human had to kill it by hand. --worker-timeout
hard-caps the iteration: the orchestrator SIGTERM/SIGKILLs the worker's whole
process group on exceed, logs the kill distinctly, then runs the SAME
post-iteration recovery as a clean exit (bd-state delta + orphan-policy).

Each test installs a fake claude that hangs (sleeps far longer than the small
--worker-timeout) and confirms grind kills it, logs the TIMEOUT, recovers from
observable bd state, and exits hands-off. The distinct "TIMEOUT" log line is
the discriminating signal: it is written ONLY on the watchdog path, never on a
clean exit, so a regression where the worker is allowed to run to natural
completion cannot satisfy these assertions.
"""

from __future__ import annotations

import json
import shutil
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
    """Returns (repo, issue_id) — one ready issue."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "worker-timeout"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "wt"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    issue_id = subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "worker-timeout test",
            "--type", "task",
            "--priority", "2",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    return repo, issue_id


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
_SLEEP_FOREVER = 'import time\nprint("hanging, no bd touch", flush=True)\ntime.sleep(120)\n'

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

# A worker that CLOSES its issue, then hangs (case 2: hung-after-close).
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
    _install_shim(monkeypatch, make_inline_python_shim(tmp_path, "claude-hang", _SLEEP_FOREVER))

    result = runner.invoke(
        app,
        [
            "grind", str(repo),
            "--iterations", "1",
            "--idle-sleep", "0",
            "--worker-timeout", "2",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    log = _grind_log(repo)
    assert "worker TIMEOUT after 2s" in log, f"watchdog should log the kill; got:\n{log}"
    # The worker never claimed anything, so bd state is unchanged.
    assert _bd_show(repo, issue_id)["status"] == "open"


def test_worker_timeout_recovers_claimed_orphan_via_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1 (stuck-alive): worker claims an issue then hangs. The watchdog
    kills it, then the normal orphan-policy=revert recovery flips the
    claimed-but-unclosed issue back to open — no human intervention."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(
        monkeypatch, make_inline_python_shim(tmp_path, "claude-claim-hang", _CLAIM_THEN_HANG)
    )

    result = runner.invoke(
        app,
        [
            "grind", str(repo),
            "--iterations", "1",
            "--idle-sleep", "0",
            "--worker-timeout", "2",
            "--orphan-policy", "revert",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _bd_show(repo, issue_id)["status"] == "open", (
        "killed worker's claim should be reverted to open by orphan-policy"
    )
    log = _grind_log(repo)
    assert "worker TIMEOUT after 2s" in log
    assert f"revert: {issue_id}" in log


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
        monkeypatch, make_inline_python_shim(tmp_path, "claude-close-hang", _CLOSE_THEN_HANG)
    )

    # Headroom: the worker runs three bd calls (ready/update/close) against
    # dolt before it starts hanging, so the timeout must comfortably exceed
    # that latency — otherwise the watchdog kills it mid-close and grind sees
    # an orphan instead of a landed close. 15s clears it; the worker still
    # hangs (sleep 120) well past it.
    result = runner.invoke(
        app,
        [
            "grind", str(repo),
            "--iterations", "1",
            "--idle-sleep", "0",
            "--worker-timeout", "15",
            "--orphan-policy", "revert",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    assert _bd_show(repo, issue_id)["status"] == "closed", (
        "a close that landed before the hang must survive the watchdog kill"
    )
    log = _grind_log(repo)
    assert "worker TIMEOUT after 15s" in log
    assert "closed +1" in log


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
        make_inline_python_shim(tmp_path, "claude-noop", 'print("did nothing", flush=True)\n'),
    )

    result = runner.invoke(
        app,
        [
            "grind", str(repo),
            "--iterations", "1",
            "--idle-sleep", "0",
            "--worker-timeout", "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "TIMEOUT" not in _grind_log(repo)
