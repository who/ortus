"""Decision table for scripts/grind_commit_guard.py (ortus-u4zv.1).

The guard is a Claude Code ``PreToolUse`` hook: it reads the tool-call JSON
on stdin and exits 2 (message on stderr) to block a git-mutating Bash command
while an ortus grind run holds the flock. Every test drives the script the
way Claude Code does — a fresh subprocess with the payload on stdin — so the
exit-code contract itself is what is pinned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

from ortus.core.grind_logic import grind_flock

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "scripts" / "grind_commit_guard.py"
TEMPLATE_GUARD = REPO_ROOT / "template" / "scripts" / "grind_commit_guard.py"
TEMPLATE_SETTINGS = (
    REPO_ROOT
    / "template"
    / "{% if agent_cli == 'claude' %}.claude{% endif %}"
    / "settings.json.jinja"
)
LIVE_SETTINGS = REPO_ROOT / ".claude" / "settings.json"

CLAIMED_ID = "ortus-test.9"


def _run_guard(
    command: str | None,
    *,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    payload: str | None = None,
    tool_name: str = "Bash",
) -> subprocess.CompletedProcess[str]:
    if payload is None:
        payload = json.dumps(
            {
                "tool_name": tool_name,
                "tool_input": {"command": command},
                "cwd": str(cwd),
            }
        )
    # Strip the marker the harness sets for its own workers: these tests may
    # themselves run inside a grind worker session, and inheriting its
    # exemption would silently allow everything.
    env = {k: v for k, v in os.environ.items() if k != "ORTUS_WORKER"}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(cwd),
        timeout=60,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "ortus.flock").touch()
    return tmp_path


_HOLDER = """
import sys, time
import portalocker
lock = portalocker.Lock(
    sys.argv[1],
    mode="a+",
    flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
    fail_when_locked=True,
)
lock.acquire()
print("held", flush=True)
time.sleep(120)
"""


@pytest.fixture()
def held(workspace: Path) -> Iterator[Path]:
    """Hold the flock the way `ortus grind` does: in a separate process.

    A sibling process, not this one — the guard exempts descendants of the
    flock holder, and holding in the test process would make every guard
    subprocess look like a grind worker.
    """
    lockfile = workspace / ".beads" / "ortus.flock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lockfile)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    try:
        yield workspace
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture()
def fake_bd_env(tmp_path: Path) -> dict[str, str]:
    """A PATH whose `bd` reports one claimed in_progress issue."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    issues = [{"id": CLAIMED_ID, "title": "fixture claimed issue"}]
    script = bin_dir / "bd"
    script.write_text(
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps("
        + repr(issues)
        + "))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    (bin_dir / "bd.bat").write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
    )
    return {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}


# --- AC-1: block + name the claimed issue ------------------------------------


def test_blocks_commit_and_names_the_claimed_issue(
    held: Path, fake_bd_env: dict[str, str]
) -> None:
    result = _run_guard("git commit -m 'intake churn'", cwd=held, extra_env=fake_bd_env)
    assert result.returncode == 2
    assert CLAIMED_ID in result.stderr
    assert "ortus.flock" in result.stderr
    # The refusal must teach the safe alternatives, not just say no.
    assert "bd" in result.stderr
    assert "read-only git" in result.stderr


def test_block_survives_bd_being_unreachable(held: Path, tmp_path: Path) -> None:
    """Issue lookup is best-effort: no usable bd still blocks, generically."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    result = _run_guard(
        "git commit -m x", cwd=held, extra_env={"PATH": str(empty_bin)}
    )
    assert result.returncode == 2
    assert "unknown" in result.stderr


# --- AC-2: mutating vs read-only decision table -------------------------------

_BLOCKED = [
    "git commit -m msg",
    "git push origin main",
    "git checkout main",
    "git switch -c fix",
    "git reset --hard HEAD~1",
    "git rebase main",
    "git merge --no-ff feature",
    "cd docs && git commit -am x",
    "git -C . commit -m x",
]

_ALLOWED = [
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git show HEAD",
    "git branch --list",
    "git log --grep merge",
    "rg 'git commit' src/",
    "bd update ortus-1 --notes 'then git push the result'",
    "echo done",
]


@pytest.mark.parametrize("command", _BLOCKED)
def test_decision_table_mutating_vs_readonly_blocks(
    held: Path, fake_bd_env: dict[str, str], command: str
) -> None:
    result = _run_guard(command, cwd=held, extra_env=fake_bd_env)
    assert result.returncode == 2, f"{command!r} should block: {result.stderr}"


@pytest.mark.parametrize("command", _ALLOWED)
def test_decision_table_mutating_vs_readonly_allows(held: Path, command: str) -> None:
    result = _run_guard(command, cwd=held)
    assert result.returncode == 0, f"{command!r} should pass: {result.stderr}"


def test_decision_table_mutating_vs_readonly_ignores_other_tools(held: Path) -> None:
    result = _run_guard("git commit -m x", cwd=held, tool_name="Edit")
    assert result.returncode == 0


# --- AC-3: nothing blocks when no run is in flight ----------------------------


def test_idle_flock_never_blocks(workspace: Path) -> None:
    result = _run_guard("git commit -m x", cwd=workspace)
    assert result.returncode == 0


def test_idle_flock_never_blocks_without_a_lockfile(tmp_path: Path) -> None:
    result = _run_guard("git commit -m x", cwd=tmp_path)
    assert result.returncode == 0


# --- AC-4: worker sessions are exempt -----------------------------------------


def test_worker_marker_exempts(held: Path) -> None:
    result = _run_guard(
        "git commit -m x", cwd=held, extra_env={"ORTUS_WORKER": "1"}
    )
    assert result.returncode == 0


@pytest.mark.skipif(
    not Path("/proc/locks").exists(), reason="ancestry exemption is Linux /proc only"
)
def test_descendant_of_flock_holder_exempts(workspace: Path) -> None:
    """A worker of a grind that predates the env marker is still not blocked.

    Holding the flock in this process makes it the guard subprocess's
    ancestor — exactly a pre-marker grind spawning a worker session.
    """
    with grind_flock(workspace):
        result = _run_guard("git commit -m x", cwd=workspace)
    assert result.returncode == 0, result.stderr


# --- AC-5: the guard degrades open on its own failures -------------------------


def test_guard_degrades_open_on_own_failure(held: Path) -> None:
    for payload in ("this is not json {{", "", '{"tool_name": "Bash"}'):
        result = _run_guard(None, cwd=held, payload=payload)
        assert result.returncode == 0, f"payload {payload!r} must not block"


# --- AC-6: ortus init template wires the hook ----------------------------------


def test_template_wires_the_hook() -> None:
    settings = TEMPLATE_SETTINGS.read_text(encoding="utf-8")
    assert '"PreToolUse"' in settings
    assert '"matcher": "Bash"' in settings
    assert "grind_commit_guard.py" in settings
    # Template consumers must receive the very same guard this repo runs.
    assert TEMPLATE_GUARD.read_bytes() == GUARD.read_bytes()


def test_live_repo_wires_the_hook() -> None:
    hooks = json.loads(LIVE_SETTINGS.read_text(encoding="utf-8"))["hooks"]
    assert any(
        "grind_commit_guard.py" in hook["command"]
        for group in hooks.get("PreToolUse", [])
        if group.get("matcher") == "Bash"
        for hook in group["hooks"]
    )
