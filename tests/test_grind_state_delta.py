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

Since ortus-pzfd.5 the close on the default path is performed by Ortus after a
fresh verifier passes the candidate, not by the worker. The closed branch and
the --tasks cap are therefore driven by a phase-aware fake runner that only
edits and emits a verdict; a worker that closed its own issue would be
rejected by the implementation isolation guard instead. The orphan branch,
where a claim goes unclosed, now belongs to the legacy `--condition` path.
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
from ortus.core.transaction import JournalStore
from tests._shims import make_inline_python_shim, normalize_git_branch, ready_issue_args


pytestmark = [pytest.mark.integration, pytest.mark.slow]
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
    normalize_git_branch(repo)
    for i in range(n_issues):
        subprocess.run(
            [
                "bd",
                "create",
                "--silent",
                "--title",
                f"delta-test-{i}",
                "--type",
                "task",
                "--priority",
                "2",
                *ready_issue_args(),
            ],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    # Ortus finalizes a verified candidate with a real `git commit`, which
    # aborts without an author identity. Runners have no global one, so the
    # fixture states its own rather than inheriting a developer machine's.
    subprocess.run(
        ["git", "config", "user.email", "ortus-tests@example.invalid"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Ortus Tests"], cwd=str(repo), check=True
    )
    return repo


def _write_shim(tmp_path: Path, name: str, body: str) -> Path:
    """Create an executable Python script that acts as a claude stand-in.

    Each shim is given the repo it should mutate via the cwd ClaudeRunner sets.
    Returns an OS-appropriate executable path (POSIX: the .py with +x;
    Windows: a generated .bat wrapper). See ortus-f4bu.
    """
    return make_inline_python_shim(tmp_path, name, body)


def _install_shim(monkeypatch: pytest.MonkeyPatch, shim: Path) -> None:
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )


def _force_fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


def _read_log(repo: Path) -> str:
    logs = sorted((repo / "logs").glob("grind-*.log"))
    return logs[-1].read_text(encoding="utf-8") if logs else ""


class _VerifiedLifecycle:
    """Phase-aware fake worker: implementation edits, verification passes.

    `readonly=True` is the verification phase — the only phase grind runs with
    a read-only posture. The fake never closes, commits, or pushes: those are
    Ortus's, and a worker that performed them would trip the isolation guard.
    """

    extra_env: dict[str, str] = {}

    def __init__(self) -> None:
        self.spawns = 0

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        readonly: bool = False,
        **kwargs: object,
    ) -> int:
        self.spawns += 1
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        if not readonly:
            (repo / f"candidate-{self.spawns}.py").write_text(f"N = {self.spawns}\n")
            return 0
        journal = JournalStore(repo).load()
        assert journal is not None
        payload = {
            "schema": 1,
            "candidate_hash": journal.candidate_hash,
            "decision": "pass",
            "criteria": [{"id": "AC-1", "status": "pass", "evidence": "reviewed"}],
            "commands": ["uv run pytest tests/test_grind_state_delta.py -q"],
            "reviewed_files": ["candidate.py"],
            "reviewed_interfaces": ["N"],
            "risks": ["none"],
            "findings": ["none"],
            "codegraph": ["fallback recorded"],
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "ORTUS_VERDICT: " + json.dumps(payload),
                        },
                    }
                )
                + "\n"
            )
        return 0


# --- closed branch --------------------------------------------------------


def test_closed_branch_when_ortus_finalizes_a_verified_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: when CLOSED_DELTA >= 1, iteration logs 'closed +N'.

    The close is Ortus's own, performed after the fresh verifier passed the
    candidate, so the delta accounting must still attribute it to the
    iteration.
    """
    repo = _seed_repo(tmp_path, n_issues=1)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    backend = _VerifiedLifecycle()
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: backend)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, (
        result.stdout + result.stderr + "\n--- log ---\n" + log
    )
    assert "closed +1" in log, f"expected closed-branch log entry; got:\n{log}"
    assert "WARN orphan" not in log
    assert "WARN no bd-state change" not in log
    assert "(report, close, commit, sync)" in log, "Ortus must own the close"


# --- orphan branch --------------------------------------------------------


def test_orphan_branch_when_subprocess_claims_without_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #4: claim-without-close path logs WARN orphan and the id.

    Driven through the legacy `--condition` path: on the default path the
    harness owns the claim and an unclosed issue is a live transaction, not an
    orphan.
    """
    repo = _seed_repo(tmp_path, n_issues=1)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)

    shim = _write_shim(
        tmp_path,
        "claude-claims-only",
        textwrap.dedent(
            """\
            import json
            import subprocess
            ready = json.loads(subprocess.run(
                ["bd", "ready", "--json"], check=True, capture_output=True, text=True
            ).stdout)
            first = next((i["id"] for i in ready if i.get("issue_type") != "epic"), None)
            if first:
                subprocess.run(
                    ["bd", "update", first, "--status", "in_progress"],
                    check=True, stdout=subprocess.DEVNULL,
                )
            print("fake-claude (orphan-branch) bailed without closing", flush=True)
            """
        ),
    )
    _install_shim(monkeypatch, shim)

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
            "--orphan-policy",
            "warn",
            "-c",
            "Close exactly one bd issue you select yourself.",
        ],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, (
        result.stdout + result.stderr + "\n--- log ---\n" + log
    )
    assert "WARN orphan claim" in log, f"expected orphan-branch log entry; got:\n{log}"
    assert "warn: orphan claim on " in log, "orphan-policy=warn should record the id"


# --- no-change branch -----------------------------------------------------


def test_no_change_branch_when_subprocess_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #5: bd-unchanged subprocess logs WARN no bd-state change.

    A true no-change (open→open) iteration only arises when the worker — not
    the harness — owns selection: the default path now claims an issue
    in-harness every iteration, so a no-op worker there yields an orphan, not
    a no-change. We exercise the no-change branch via the legacy `--condition`
    path (`-c`), where the harness does NOT pre-claim and a no-op worker leaves
    bd state untouched.
    """
    repo = _seed_repo(tmp_path, n_issues=1)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)

    shim = _write_shim(
        tmp_path,
        "claude-no-op",
        textwrap.dedent(
            """\
            print("fake-claude (no-op) did nothing", flush=True)
            """
        ),
    )
    _install_shim(monkeypatch, shim)

    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "-c",
            "do nothing",
            "--iterations",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, (
        result.stdout + result.stderr + "\n--- log ---\n" + log
    )
    assert "WARN no bd-state change" in log, (
        f"expected no-change branch log entry; got:\n{log}"
    )


# --- queue exhaustion (acceptance #6) ------------------------------------


def test_human_only_queue_treated_as_drained_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-9db5: when every remaining issue is labeled `human`, the
    orchestrator must treat the queue as drained instead of spinning."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "human-only"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "hu"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    # Seed two issues, label both `human` so nothing is agent-claimable.
    for i in range(2):
        new_id = subprocess.run(
            [
                "bd",
                "create",
                "--silent",
                "--title",
                f"human-only-{i}",
                "--type",
                "task",
                "--priority",
                "2",
                "--labels",
                "human",
            ],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_id  # sanity
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
    assert spawn_count["n"] == 0, (
        "expected zero claude spawns when every issue is human-flagged"
    )
    log = _read_log(repo)
    assert "queue already drained" in log


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
    normalize_git_branch(repo)
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
    """Acceptance #8: --tasks N exits cleanly after N bd-state-verified closes.

    "Verified" is now literal: each close is Ortus finalizing a candidate a
    fresh verifier passed, so the cap counts completed transactions.
    """
    repo = _seed_repo(tmp_path, n_issues=3)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    backend = _VerifiedLifecycle()
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: backend)

    result = runner.invoke(
        app,
        ["grind", str(repo), "--tasks", "2", "--idle-sleep", "0"],
    )
    log = _read_log(repo)
    assert result.exit_code == 0, (
        result.stdout + result.stderr + "\n--- log ---\n" + log
    )
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
