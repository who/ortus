"""How many bd processes one grind window is allowed to spawn (ortus-acaz).

Every ``BdClient`` method is a fresh ``bd`` process opening the embedded Dolt
database, about a second of it on a CI runner, and a grind window that reads
the same view twice pays that cost twice. These two tests are the budget: a
counting ``bd`` is prepended to ``PATH`` and every invocation the window makes
— the harness's and the worker's alike — appends one line to a log.

The ceilings are half of what the same scenarios measured before the snapshot
read path existed, which is the bar the work spec sets. The template workspace
is copied before the counter is installed, so a session's one-time template
build is never in the count.

Measured counts, on this repository's fixtures:

- leftover claim: 31 before, 14 after, ceiling 15.
- flagged-claim reap: 44 before, 21 after, ceiling 22.

A ceiling is not a target to be met by asking the tracker less often than a
decision needs. Each of these windows still reads fresh state wherever a
worker could have changed it; what went away is the second read of one view
inside a single decision.
"""

from __future__ import annotations

import json
import os
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
from tests.conftest import copy_bd_workspace

runner = CliRunner()

#: Half of the 31 invocations the leftover-claim window made before the change.
LEFTOVER_CEILING = 15

#: Half of the 44 invocations the flagged-claim reap made before the change.
REAP_CEILING = 22


def _install_bd_counter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepend a counting `bd` to PATH; return the log it appends to.

    The real binary is resolved first and baked into the shim as an absolute
    path, so the shim cannot find itself and the count survives a worker whose
    environment differs from this process's.
    """

    real = shutil.which("bd")
    if real is None:
        pytest.skip("bd not on PATH")
    bin_dir = tmp_path / "counting-bin"
    bin_dir.mkdir()
    log = tmp_path / "bd-calls.log"
    shim = bin_dir / "bd"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$*" >> "{log}"\n'
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def _calls(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _seed(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A committed workspace holding one ready leaf, built before counting."""

    workspace = copy_bd_workspace(tmp_path / name, "leaf")
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


def _stub_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


class _ClaimAndBailRunner:
    """The worker of the leave-claim-in-progress scenario: claims, then exits."""

    extra_env: dict[str, str] = {}

    def __init__(self, host: Path) -> None:
        self.host = host

    def run(self, prompt: str, **kwargs: object) -> int:
        listing = json.loads(
            subprocess.run(
                ["bd", "list", "--status=in_progress", "--json"],
                cwd=self.host,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        if listing:
            return 0
        ready = json.loads(
            subprocess.run(
                ["bd", "ready", "--json"],
                cwd=self.host,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        issue_id = next(
            item["id"] for item in ready if item.get("issue_type") != "epic"
        )
        subprocess.run(
            ["bd", "update", issue_id, "--status=in_progress"],
            cwd=self.host,
            check=True,
            capture_output=True,
        )
        return 0


# The worker of the flagged-claim reap scenario: it claims its issue, labels it
# human the way the PLAN-GAP exit does, and then hangs like a worker the /goal
# Stop hook will not let stop.
_FLAG_THEN_HANG = textwrap.dedent(
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
            ["bd", "label", "add", first, "human"],
            check=True, stdout=subprocess.DEVNULL,
        )
        print(f"flagged {first} human, now held by the hook", flush=True)
    time.sleep(120)
    """
)


@pytest.mark.integration
@pytest.mark.slow
def test_leftover_claim_window_stays_under_its_bd_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: one window that leaves a claim in progress, counted end to end."""

    repo, issue_id = _seed(tmp_path, "leftover")
    _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _ClaimAndBailRunner(repo)
    )
    log = _install_bd_counter(tmp_path, monkeypatch)

    result = runner.invoke(
        app, ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    calls = _calls(log)
    assert calls, "the counting bd was never reached"
    grind_log = "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )
    assert f"left {issue_id} in_progress" in grind_log, grind_log
    assert len(calls) <= LEFTOVER_CEILING, (
        f"{len(calls)} bd invocations exceeds the {LEFTOVER_CEILING} ceiling:\n"
        + "\n".join(calls)
    )


@pytest.mark.integration
@pytest.mark.slow
def test_flagged_claim_reap_stays_under_its_bd_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: the reap poll's own window, counted the same way."""

    repo, issue_id = _seed(tmp_path, "reap")
    _stub_host(monkeypatch, tmp_path)
    shim = make_inline_python_shim(tmp_path, "claude-flag-hang", _FLAG_THEN_HANG)
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: ClaudeRunner(claude_binary=str(shim))
    )
    log = _install_bd_counter(tmp_path, monkeypatch)

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
            "90",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    calls = _calls(log)
    grind_log = "\n".join(
        path.read_text(encoding="utf-8") for path in (repo / "logs").glob("grind-*.log")
    )
    assert f"claim flagged human ({issue_id}); reaping worker" in grind_log, grind_log
    assert "TIMEOUT" not in grind_log, grind_log
    assert len(calls) <= REAP_CEILING, (
        f"{len(calls)} bd invocations exceeds the {REAP_CEILING} ceiling:\n"
        + "\n".join(calls)
    )
