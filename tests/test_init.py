"""Tests for ortus init (q075.5 acceptance criteria).

Marked integration since they shell out to real `bd init`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture(autouse=True)
def _require_bd() -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd binary not on PATH")


def test_init_on_empty_dir_creates_all_artifacts(tmp_path: Path) -> None:
    """Acceptance #1: fresh dir → .beads/, settings.json, .ortusrc, AGENTS.md, .gitignore."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".beads").is_dir()
    assert (target / ".claude" / "settings.json").is_file()
    assert (target / ".ortusrc").is_file()
    assert (target / "AGENTS.md").is_file()
    assert (target / ".gitignore").is_file()


def test_settings_json_has_bd_excluded_and_hooks(tmp_path: Path) -> None:
    """Acceptance #2: settings has sandbox.excludedCommands and bd-prime hooks."""
    target = tmp_path / "fresh"
    runner.invoke(app, ["init", str(target)])
    data = json.loads((target / ".claude" / "settings.json").read_text())
    assert "bd" in data["sandbox"]["excludedCommands"]
    assert "bd *" in data["sandbox"]["excludedCommands"]
    hooks = data["hooks"]
    assert any(
        h["command"] == "bd prime"
        for group in hooks.get("SessionStart", [])
        for h in group["hooks"]
    )
    assert any(
        h["command"] == "bd prime"
        for group in hooks.get("PreCompact", [])
        for h in group["hooks"]
    )


def test_init_refuses_existing_beads_without_force(tmp_path: Path) -> None:
    """Acceptance #3: existing .beads/ → exit 1 without --force."""
    target = tmp_path / "exists"
    target.mkdir()
    (target / ".beads").mkdir()
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert "already has a .beads/" in (result.stdout + result.stderr)


def test_init_force_rerenders_templates(tmp_path: Path) -> None:
    """Acceptance #4: --force re-renders ortus-owned files."""
    target = tmp_path / "fresh"
    runner.invoke(app, ["init", str(target)])
    settings = target / ".claude" / "settings.json"
    settings.write_text('{"corrupted": true}')
    assert json.loads(settings.read_text()) == {"corrupted": True}
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(settings.read_text())
    assert "sandbox" in data, "settings.json should be re-rendered"


def test_prefix_is_respected(tmp_path: Path) -> None:
    """Acceptance #5: --prefix foo causes bd issues to carry foo- prefix."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target), "--prefix", "myfeat"])
    assert result.exit_code == 0
    # Create an issue with bd; its id should start with the prefix.
    proc = subprocess.run(
        ["bd", "create", "--silent", "--title", "smoke", "--type", "task"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=True,
    )
    new_id = proc.stdout.strip()
    assert new_id.startswith("myfeat-"), f"got id {new_id!r}, expected myfeat- prefix"


def test_default_prefix_is_dir_basename(tmp_path: Path) -> None:
    target = tmp_path / "fancyname"
    runner.invoke(app, ["init", str(target)])
    proc = subprocess.run(
        ["bd", "create", "--silent", "--title", "smoke", "--type", "task"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip().startswith("fancyname-")


def test_init_under_five_seconds(tmp_path: Path) -> None:
    """Acceptance #6 (NFR-001): wall-clock ≤ 5s on a typical host."""
    target = tmp_path / "perf"
    t0 = time.monotonic()
    result = runner.invoke(app, ["init", str(target)])
    elapsed = time.monotonic() - t0
    assert result.exit_code == 0
    assert elapsed < 5.0, f"ortus init took {elapsed:.2f}s (NFR-001 budget: 5s)"


def test_ortusrc_round_trips_as_toml(tmp_path: Path) -> None:
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    target = tmp_path / "fresh"
    runner.invoke(app, ["init", str(target), "--prefix", "abc", "--project-type", "go"])
    data = tomllib.loads((target / ".ortusrc").read_text())
    assert data["prefix"] == "abc"
    assert data["project_type"] == "go"
