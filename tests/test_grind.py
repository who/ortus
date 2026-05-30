"""Integration-ish tests for ortus grind (xvel.4 acceptance)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.sandbox import SandboxInfo
from tests._shims import normalize_git_branch, shim_path

runner = CliRunner()

FAKE_CLAUDE = shim_path("fake-claude")


def _fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _fixture_repo(tmp_path: Path) -> Path:
    """Repo with .beads/ + .claude/settings.json with hooks enabled."""
    repo = tmp_path / "fixture"
    (repo / ".beads").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    return repo


def test_grind_dry_run_prints_resolved_flags_and_exits(
    tmp_path: Path,
) -> None:
    """Dry-run path: no sandbox/hook/flock work; just emit the resolved state."""
    repo = _fixture_repo(tmp_path)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run", "--tasks", "1"])
    assert result.exit_code == 0
    assert "repo:" in result.stdout
    assert "tasks:" in result.stdout
    assert "/goal" in result.stdout


def test_dry_run_startup_under_500ms(tmp_path: Path) -> None:
    """NFR-002: startup overhead ≤ 500ms (measured via --dry-run as a proxy)."""
    repo = _fixture_repo(tmp_path)
    t0 = time.monotonic()
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    elapsed = time.monotonic() - t0
    assert result.exit_code == 0
    assert elapsed < 0.5, f"grind --dry-run took {elapsed*1000:.0f}ms (NFR-002 budget: 500ms)"


def test_grind_exits_one_on_missing_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fixture_repo(tmp_path)

    def _boom() -> None:
        raise sandbox_mod.SandboxUnavailable("Sandbox prerequisite missing: bubblewrap (bwrap)\n  hint")

    monkeypatch.setattr(sandbox_mod, "smoke_test", _boom)
    result = runner.invoke(app, ["grind", str(repo)])
    assert result.exit_code == 1
    assert "bubblewrap" in (result.stdout + result.stderr)


def test_grind_exits_one_on_disabled_hooks_before_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: disableAllHooks=true → exit 1 BEFORE any claude spawn."""
    repo = _fixture_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"disableAllHooks": True, "sandbox": {"excludedCommands": ["bd", "bd *"]}})
    )
    _fake_sandbox(monkeypatch)
    # Force home so the user's real ~/.claude isn't checked.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))

    # If claude DID spawn, our test would hang waiting on the fake-claude shim.
    # So make _make_runner raise to assert it's never called.
    def _should_not_be_called() -> ClaudeRunner:
        raise AssertionError("claude was spawned despite disableAllHooks=true")

    monkeypatch.setattr(grind_mod, "_make_runner", _should_not_be_called)

    result = runner.invoke(app, ["grind", str(repo)])
    assert result.exit_code == 1
    assert "disableAllHooks" in (result.stdout + result.stderr) or "hooks" in (result.stdout + result.stderr)


def test_grind_runs_fake_claude_and_logs_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: with a fake claude that exits 0, grind runs one iteration and writes a log.

    Updated for ortus-3ico subprocess-per-task shape: the loop now spawns
    one claude per iteration, so we seed a single ready issue and cap with
    --iterations 1 --idle-sleep 0 so the fake-claude (which doesn't touch
    bd) doesn't trigger an infinite no-change retry.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "fixtg"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    # Seed one ready issue so queue_drained() doesn't short-circuit before
    # claude is spawned.
    subprocess.run(
        ["bd", "create", "--silent", "--title", "smoke task", "--type", "task", "--priority", "2"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(FAKE_CLAUDE)))

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    log_dir = repo / "logs"
    assert log_dir.is_dir()
    logs = list(log_dir.glob("grind-*.log"))
    assert logs, "expected a grind-*.log under logs/"
    # The fake-claude shim writes "fake-claude done" to its stdout, which gets
    # tee'd to log_path by ClaudeRunner.run.
    assert any("fake-claude done" in p.read_text(encoding="utf-8") for p in logs)


def test_grind_harness_selects_claims_and_injects_issue_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-xo1u: the harness (not the worker) selects + claims the next ready
    issue and injects its EXACT id into the per-iteration /goal prompt.

    With a fake claude that echoes its argv but never touches bd, we can assert
    the worker was handed the specific id the harness claimed — proving the
    worker is TOLD which issue to work rather than choosing/transcribing it.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "fixth"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    normalize_git_branch(repo)
    create = subprocess.run(
        ["bd", "create", "--silent", "--title", "inject me", "--type", "task", "--priority", "1"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    issue_id = create.stdout.strip()
    assert issue_id, "expected bd create to print the new id"
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))

    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(FAKE_CLAUDE))
    )

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    logs = list((repo / "logs").glob("grind-*.log"))
    assert logs
    log_text = "\n".join(p.read_text(encoding="utf-8") for p in logs)
    # The harness logged the in-harness select+claim of the EXACT id...
    assert f"harness selected+claimed {issue_id}" in log_text
    # ...and the worker's prompt (echoed by fake-claude's argv) carried that id.
    assert f"Work bd issue {issue_id}" in log_text


def test_grind_dry_run_default_shows_harness_select(tmp_path: Path) -> None:
    """Default (no --condition) dry-run advertises harness-side selection and
    the work-issue template with its placeholders intact."""
    repo = _fixture_repo(tmp_path)
    result = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert result.exit_code == 0
    assert "select:" in result.stdout
    assert "harness" in result.stdout
    assert "<ISSUE_ID>" in result.stdout


def test_grind_fr003_no_beads(tmp_path: Path) -> None:
    bogus = tmp_path / "no-beads"
    bogus.mkdir()
    result = runner.invoke(app, ["grind", str(bogus)])
    assert result.exit_code == 1
