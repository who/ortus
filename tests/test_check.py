"""Tests for ortus check (q075.6 acceptance criteria)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import check as check_mod
from ortus.core.readiness import READINESS_MEMORY_KEY, readiness_memory_text

runner = CliRunner()


# --- fixture helpers -------------------------------------------------------


def _fake_bd_run(*, readiness_memory: bool):
    """Stand in for subprocess.run for every binary `check` shells out to.

    `bd memories --json` answers with a memory map; everything else answers
    with a version line, which is all `_binary_check` reads.
    """
    memories: dict[str, object] = {"schema_version": 1}
    if readiness_memory:
        memories[READINESS_MEMORY_KEY] = readiness_memory_text()

    class _CP:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def _run(args, *a, **k):
        if "memories" in args:
            return _CP(json.dumps(memories))
        return _CP("fake 1.0.0\n")

    return _run


def _healthy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "healthy"
    (repo / ".beads").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}})
    )
    return repo


def _all_binaries_present(
    monkeypatch: pytest.MonkeyPatch, *, readiness_memory: bool = True
) -> None:
    """Pretend bd, claude, jq are on PATH and return a version string."""
    import subprocess as _sp

    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(_sp, "run", _fake_bd_run(readiness_memory=readiness_memory))


def _fake_sandbox_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from ortus.core.sandbox import SandboxInfo

    monkeypatch.setattr(
        check_mod.sandbox,
        "smoke_test",
        lambda: SandboxInfo(platform="Linux", binary="bwrap"),
    )
    # The verifier preflight shells out; these tests replace subprocess.run
    # wholesale, so a healthy posture has to be stated rather than executed.
    monkeypatch.setattr(
        check_mod.ClaudeRunner,
        "preflight_readonly",
        lambda self, repo, **kwargs: None,
    )


# --- acceptance tests ------------------------------------------------------


def test_check_all_green_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #1: healthy repo → exit 0."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    assert "FAIL" not in result.stdout


def test_check_fails_on_disabled_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #2: disableAllHooks=true → exit 1 with clear FAIL."""
    repo = _healthy_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "disableAllHooks": True,
                "sandbox": {"excludedCommands": ["bd", "bd *"]},
            }
        )
    )
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    # Force home to a tmp dir so the user's real ~/.claude isn't checked.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "hooks" in result.stdout


def test_check_fails_on_missing_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: bwrap missing → exit 1 with sandbox FAIL."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    from ortus.core.sandbox import SandboxUnavailable

    def _boom() -> None:
        raise SandboxUnavailable("Sandbox prerequisite missing: bubblewrap (bwrap)\n  install hint")

    monkeypatch.setattr(check_mod.sandbox, "smoke_test", _boom)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "sandbox" in result.stdout
    assert "FAIL" in result.stdout


def test_check_reports_a_verifier_sandbox_that_cannot_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-dyio AC-2: the blocked-execution condition is visible before a run."""
    from ortus.core.claude import ReadOnlyExecutionBlocked

    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)

    def _blocked(self, repo: Path, **kwargs: object) -> None:
        raise ReadOnlyExecutionBlocked(
            "read-only verifier execution probe failed: mkdir: cannot create "
            "directory: Read-only file system\n  agent session-env: /nowhere"
        )

    monkeypatch.setattr(check_mod.ClaudeRunner, "preflight_readonly", _blocked)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    # The table wraps long cells across rows, so drop the borders too.
    compact = "".join(c for c in result.stdout if not c.isspace() and c != "│")
    assert "verifiersandbox" in compact
    assert "executionprobefailed" in compact
    assert "Read-onlyfilesystem" in compact


def test_check_skips_the_verifier_probe_for_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Codex verifier is unwrapped, so there is no posture to probe."""
    repo = tmp_path / "codex-probe"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        'sandbox_mode = "workspace-write"\napproval_policy = "never"\n'
    )
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)

    def _never(self, repo: Path, **kwargs: object) -> None:
        raise AssertionError("the Codex backend must not run the Claude preflight")

    monkeypatch.setattr(check_mod.ClaudeRunner, "preflight_readonly", _never)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "verifier sandbox" not in result.stdout


def _snapshot_mtimes(root: Path) -> dict[str, tuple[float, int]]:
    snap: dict[str, tuple[float, int]] = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p)] = (st.st_mtime, st.st_size)
    return snap


def test_check_makes_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #4 + #5: NFR-006 read-only — no filesystem mutations."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    before = _snapshot_mtimes(repo)
    runner.invoke(app, ["check", str(repo)])
    after = _snapshot_mtimes(repo)
    assert before == after, "check must be strictly read-only (NFR-006)"


def test_check_reports_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _healthy_repo(tmp_path)
    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: None)

    def _absent(args, *a, **k):
        raise FileNotFoundError(f"no such binary: {args[0]}")

    monkeypatch.setattr(check_mod.subprocess, "run", _absent)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "bd" in result.stdout and "FAIL" in result.stdout


def test_check_reports_missing_excluded_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _healthy_repo(tmp_path)
    # Wipe excludedCommands so the check fails.
    (repo / ".claude" / "settings.json").write_text(json.dumps({}))
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "excludedCommands" in result.stdout or "sandbox" in result.stdout


def test_check_reports_missing_beads_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "no-beads"
    repo.mkdir()
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert ".beads/" in result.stdout


def test_check_reports_prompt_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "grind-prompt.md").write_text("custom")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0
    assert "grind-prompt.md" in result.stdout


def test_check_reports_stale_override_missing_the_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: an override predating $readiness_spec is reported, not failed."""
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "plan-prompt.md").write_text("frozen contract, no placeholder\n")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    # Rich wraps long cells, so compare with whitespace removed.
    compact = "".join(result.stdout.split())
    assert "stale" in compact
    assert "$readiness_spec" in compact


def test_check_leaves_a_current_override_out_of_the_stale_override_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override carrying the placeholder is an ordinary override."""
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "plan-prompt.md").write_text("custom preamble\n$readiness_spec\n")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = "".join(result.stdout.split())
    assert "plan-prompt.md" in compact
    assert "stale" not in compact


def test_check_reports_present_readiness_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the pointer memory is reported as present when bd has it."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # The table wraps long cells, so compare with whitespace removed.
    compact = "".join(result.stdout.split())
    assert f"key={READINESS_MEMORY_KEY}" in compact
    assert "FAIL" not in result.stdout


def test_check_reports_missing_readiness_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a workspace without the pointer fails and names the add command."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch, readiness_memory=False)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    # The table wraps long cells, so compare with whitespace removed.
    compact = "".join(result.stdout.split())
    assert "bdremember" in compact
    assert f"--key{READINESS_MEMORY_KEY}" in compact


def test_check_readiness_memory_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-006: the memory query must not take any bd write path."""
    repo = _healthy_repo(tmp_path)
    seen: list[list[str]] = []
    real = _fake_bd_run(readiness_memory=True)

    def _record(args, *a, **k):
        seen.append(list(args))
        return real(args, *a, **k)

    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(check_mod.subprocess, "run", _record)
    _fake_sandbox_ok(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    runner.invoke(app, ["check", str(repo)])
    memory_calls = [args for args in seen if "memories" in args]
    assert memory_calls, seen
    for args in memory_calls:
        assert "--readonly" in args and "--sandbox" in args, args
        assert "remember" not in args, args


def test_check_codex_uses_codex_binary_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "codex-project"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        'sandbox_mode = "workspace-write"\napproval_policy = "never"\n'
    )
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    seen: list[str] = []

    def which(binary: str) -> str:
        seen.append(binary)
        return f"/usr/bin/{binary}"

    monkeypatch.setattr(check_mod.shutil, "which", which)
    monkeypatch.setattr(
        check_mod.subprocess, "run", _fake_bd_run(readiness_memory=True)
    )
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "codex" in seen
    assert "claude" not in seen
    assert ".codex/config.toml" in result.stdout
    assert "hooks" not in result.stdout
