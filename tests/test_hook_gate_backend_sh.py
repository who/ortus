"""Tests for the FR-008 hook-gate guard in ortus/goal.sh.

`check_hooks_enabled` enforces Claude's managed-Stop-hook requirement: if
disableAllHooks=true is set anywhere in the settings stack, /goal degrades into
a hookless `claude -p` run and the launch is refused. Under codex, /goal is
native to the CLI and reads none of those settings files, so the gate must be
skipped rather than weakened.

Each case extracts the real function and its real call site out of goal.sh and
runs them in a fresh bash, so the assertions track the shipped file's text
rather than a paraphrase of it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._platform import skip_on_windows_bash_shim

pytestmark = skip_on_windows_bash_shim

REPO_ROOT = Path(__file__).parent.parent
BASH = shutil.which("bash") or "/bin/bash"
GOAL_SH = REPO_ROOT / "ortus" / "goal.sh"
TEMPLATE_GOAL_SH = REPO_ROOT / "template" / "ortus" / "goal.sh"


def extract_block(text: str, start: str, end: str) -> str:
    """Slice goal.sh from the line beginning `start` through the line `end`."""
    lines = text.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith(start))
    last = next(i for i, ln in enumerate(lines[first:], first) if ln == end)
    return "\n".join(lines[first : last + 1])


def harness(goal_sh: Path) -> str:
    """The gate's definition + its guarded call site, lifted verbatim."""
    text = goal_sh.read_text()
    return "\n".join(
        [
            'log() { echo "$*"; }',
            extract_block(text, "check_hooks_enabled() {", "}"),
            extract_block(text, 'if [ "$ORTUS_BACKEND" = "claude" ]; then', "fi"),
            "echo REACHED_LAUNCH",
        ]
    )


def run_gate(
    tmp_path: Path, goal_sh: Path, *, backend: str, disable_all_hooks: bool
) -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    repo.mkdir()
    if disable_all_hooks:
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(
            json.dumps({"disableAllHooks": True})
        )
    return subprocess.run(
        [BASH, "-c", harness(goal_sh)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "ORTUS_BACKEND": backend},
    )


@pytest.fixture(params=["repo", "template"])
def goal_sh(request: pytest.FixtureRequest) -> Path:
    """Both mirrors must carry the guard; the template one is what ships."""
    return {"repo": GOAL_SH, "template": TEMPLATE_GOAL_SH}[request.param]


def test_codex_skips_the_gate_when_hooks_are_disabled(
    tmp_path: Path, goal_sh: Path
) -> None:
    """The FR-008 case: a config that blocks claude must not block codex."""
    proc = run_gate(tmp_path, goal_sh, backend="codex", disable_all_hooks=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_LAUNCH" in proc.stdout
    assert "disableAllHooks" not in proc.stdout
    assert "Hook precheck: skipped (backend=codex" in proc.stdout


def test_codex_skips_the_gate_with_no_settings_at_all(
    tmp_path: Path, goal_sh: Path
) -> None:
    proc = run_gate(tmp_path, goal_sh, backend="codex", disable_all_hooks=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_LAUNCH" in proc.stdout
    # Skipped, not silently passed through the claude-only wording.
    assert "Hook precheck: enabled" not in proc.stdout


def test_claude_still_refuses_when_hooks_are_disabled(
    tmp_path: Path, goal_sh: Path
) -> None:
    """The guard narrows who runs the gate, never what the gate decides."""
    proc = run_gate(tmp_path, goal_sh, backend="claude", disable_all_hooks=True)
    assert proc.returncode == 1
    assert "REACHED_LAUNCH" not in proc.stdout
    assert "disableAllHooks=true" in proc.stdout


def test_claude_passes_when_hooks_are_enabled(tmp_path: Path, goal_sh: Path) -> None:
    proc = run_gate(tmp_path, goal_sh, backend="claude", disable_all_hooks=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_LAUNCH" in proc.stdout
    assert "Hook precheck: enabled" in proc.stdout
