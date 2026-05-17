"""Integration tests for the bd-state-delta branches in `ortus grind`
(ortus-3ico acceptance #3 + #4 + #5).

Each test:
 - seeds a tiny bd workspace,
 - swaps in a fake claude shim that mutates bd in a specific way
   (closes / claims / no-op),
 - invokes `ortus grind --iterations 1 --idle-sleep 0`,
 - asserts the log records the expected branch.

The outer loop is verified against observable bd state, not model
claims or transcript content — that's the whole point of the pivot.
"""

from __future__ import annotations

import json
import shutil
import stat
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


pytestmark = pytest.mark.integration
runner = CliRunner()


def _stub_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _seed_repo(tmp_path: Path, n_issues: int = 1) -> Path:
    """A bd-initialized repo with `n_issues` ready tasks and an enabled .claude."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "sdt"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    for i in range(n_issues):
        subprocess.run(
            [
                "bd", "create", "--silent",
                "--title", f"delta-test-{i}",
                "--type", "task",
                "--priority", "2",
            ],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    return repo


def _write_shim(tmp_path: Path, name: str, body: str) -> Path:
    """Create an executable bash script that acts as a claude stand-in.

    Each shim is given the repo it should mutate via the cwd ClaudeRunner sets.
    """
    shim = tmp_path / name
    shim.write_text(body)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _install_shim(monkeypatch: pytest.MonkeyPatch, shim: Path) -> None:
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )


def _force_fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


def _read_log(repo: Path) -> str:
    logs = sorted((repo / "logs").glob("grind-*.log"))
    return logs[-1].read_text() if logs else ""


# --- closed branch --------------------------------------------------------


def test_closed_branch_when_subprocess_closes_an_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: when CLOSED_DELTA >= 1, iteration logs 'closed +N'."""
    repo = _seed_repo(tmp_path, n_issues=1)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)

    shim = _write_shim(
        tmp_path,
        "claude-closes-one.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -e
            # Close the first ready issue — mirrors what a real claude session
            # would do at the end of a successful close-one /goal turn.
            first=$(bd ready --json | python3 -c "
            import json, sys
            d = json.load(sys.stdin)
            for i in d:
                if i.get('issue_type') != 'epic':
                    print(i['id'])
                    break
            ")
            bd close "$first" --reason "delta-test closed branch" >/dev/null
            echo "fake-claude (closed-branch) done"
            exit 0
            """
        ),
    )
    _install_shim(monkeypatch, shim)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, result.stdout + result.stderr + "\n--- log ---\n" + log
    assert "closed +1" in log, f"expected closed-branch log entry; got:\n{log}"
    assert "WARN orphan" not in log
    assert "WARN no bd-state change" not in log


# --- orphan branch --------------------------------------------------------


def test_orphan_branch_when_subprocess_claims_without_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #4: claim-without-close path logs WARN orphan and the id."""
    repo = _seed_repo(tmp_path, n_issues=1)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)

    shim = _write_shim(
        tmp_path,
        "claude-claims-only.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -e
            first=$(bd ready --json | python3 -c "
            import json, sys
            d = json.load(sys.stdin)
            for i in d:
                if i.get('issue_type') != 'epic':
                    print(i['id'])
                    break
            ")
            bd update "$first" --status in_progress >/dev/null
            echo "fake-claude (orphan-branch) bailed without closing"
            exit 0
            """
        ),
    )
    _install_shim(monkeypatch, shim)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, result.stdout + result.stderr + "\n--- log ---\n" + log
    assert "WARN orphan claim" in log, f"expected orphan-branch log entry; got:\n{log}"
    assert "warn: orphan claim on " in log, "default orphan-policy=warn should record the id"


# --- no-change branch -----------------------------------------------------


def test_no_change_branch_when_subprocess_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #5: bd-unchanged subprocess logs WARN no bd-state change."""
    repo = _seed_repo(tmp_path, n_issues=1)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)

    shim = _write_shim(
        tmp_path,
        "claude-no-op.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            echo "fake-claude (no-op) did nothing"
            exit 0
            """
        ),
    )
    _install_shim(monkeypatch, shim)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, result.stdout + result.stderr + "\n--- log ---\n" + log
    assert "WARN no bd-state change" in log, f"expected no-change branch log entry; got:\n{log}"


# --- queue exhaustion (acceptance #6) ------------------------------------


def test_queue_drained_exits_outer_loop_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #6: when bd shows zero open + zero in_progress at startup,
    the outer loop never spawns claude."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "empty-queue"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "eq"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)

    spawn_count = {"n": 0}

    class _SpyRunner:
        extra_env: dict[str, str] = {}

        def run(self, *args, **kwargs) -> int:
            spawn_count["n"] += 1
            return 0

    monkeypatch.setattr(grind_mod, "_make_runner", lambda: _SpyRunner())

    result = runner.invoke(app, ["grind", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert spawn_count["n"] == 0, "expected zero claude spawns on a drained queue"
    log = _read_log(repo)
    assert "queue already drained" in log


# --- --tasks cap (acceptance #8) -----------------------------------------


def test_tasks_cap_stops_outer_loop_after_n_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #8: --tasks N exits cleanly after N bd-state-verified closes."""
    repo = _seed_repo(tmp_path, n_issues=3)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)

    shim = _write_shim(
        tmp_path,
        "claude-closes-one-each.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -e
            first=$(bd ready --json | python3 -c "
            import json, sys
            d = json.load(sys.stdin)
            for i in d:
                if i.get('issue_type') != 'epic':
                    print(i['id'])
                    break
            ")
            bd close "$first" --reason "tasks-cap test" >/dev/null
            echo "closed $first"
            exit 0
            """
        ),
    )
    _install_shim(monkeypatch, shim)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--tasks", "2", "--idle-sleep", "0"],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, result.stdout + result.stderr + "\n--- log ---\n" + log
    assert "--tasks cap reached: 2/2" in log, f"expected tasks-cap exit; got:\n{log}"
    # One issue should remain open (3 seeded, 2 closed under cap).
    proc = subprocess.run(
        ["bd", "count", "--status", "open", "--json"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(proc.stdout)["count"] == 1
