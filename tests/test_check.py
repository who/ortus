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

runner = CliRunner()


# --- fixture helpers -------------------------------------------------------


def _healthy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "healthy"
    (repo / ".beads").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}})
    )
    return repo


def _all_binaries_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend bd, claude, jq are on PATH and return a version string."""
    import subprocess as _sp

    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    class _CP:
        def __init__(self) -> None:
            self.stdout = "fake 1.0.0\n"
            self.stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _CP())


def _fake_sandbox_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from ortus.core.sandbox import SandboxInfo

    monkeypatch.setattr(
        check_mod.sandbox,
        "smoke_test",
        lambda: SandboxInfo(platform="Linux", binary="bwrap"),
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
