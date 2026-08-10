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
from ortus.core.readiness import READINESS_MEMORY_KEY

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture(autouse=True)
def _require_bd() -> None:
    if shutil.which("bd") is None:
        pytest.skip("bd binary not on PATH")


@pytest.fixture(autouse=True)
def _fake_codegraph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep init's CodeGraph bootstrap hermetic.

    Init now defaults to `--codegraph required`, so every invocation below
    would otherwise shell out to a real `codegraph init` — slow, and a hard
    failure on a host without the CLI. The fake stands in for both the PATH
    lookup and the index build.
    """
    import ortus.commands.init as init_mod

    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: "/usr/bin/codegraph")
    monkeypatch.setattr(
        init_mod,
        "_codegraph_index",
        lambda repo, **kwargs: (repo / ".codegraph").mkdir(parents=True, exist_ok=True),
    )


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
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "main"


def test_init_codex_creates_only_codex_config(tmp_path: Path) -> None:
    target = tmp_path / "codex"
    result = runner.invoke(app, ["init", str(target), "--backend", "codex"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".codex" / "config.toml").is_file()
    assert not (target / ".claude").exists()
    assert 'backend = "codex"' in (target / ".ortusrc").read_text()


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


def _bd_memories(repo: Path) -> dict:
    proc = subprocess.run(
        ["bd", "memories", "--json"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_init_stores_readiness_memory(tmp_path: Path) -> None:
    """AC-1: the keyed pointer memory lands in the new bd workspace."""
    target = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    memories = _bd_memories(target)
    assert READINESS_MEMORY_KEY in memories, sorted(memories)
    assert "ortus spec" in memories[READINESS_MEMORY_KEY]


@pytest.mark.slow
def test_init_force_does_not_duplicate_readiness_memory(tmp_path: Path) -> None:
    """Re-running init over an existing workspace updates the memory in place."""
    target = tmp_path / "fresh"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    memories = _bd_memories(target)
    matching = [k for k in memories if k.startswith(READINESS_MEMORY_KEY)]
    assert matching == [READINESS_MEMORY_KEY], matching


def test_init_readiness_memory_failure_warns_and_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a bd too old for `remember` costs a warning, not the bootstrap."""
    import ortus.commands.init as init_mod

    def fake_run(args, **kwargs):
        if args[:2] == ["bd", "remember"]:
            raise subprocess.CalledProcessError(returncode=1, cmd=args)
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(init_mod.subprocess, "run", fake_run)
    target = tmp_path / "oldbd"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    compact = "".join(combined.split())
    assert "couldnotstorethereadinessmemory" in compact, combined
    assert "bdremember" in compact, combined


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


def test_init_surfaces_bd_failure_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-btt3: when bd init exits non-zero its output streams straight to
    the operator, and ortus exits 1 with a clear `bd init failed (exit N)` line.
    """
    import ortus.commands.init as init_mod

    def fake_run(args, cwd, check):  # noqa: ARG001 — match subprocess.run signature
        # bd's own stderr would normally print here; the wrapper trusts that
        # the operator already saw it and just signals the failure.
        raise subprocess.CalledProcessError(returncode=7, cmd=args)

    monkeypatch.setattr(init_mod.subprocess, "run", fake_run)
    target = tmp_path / "doomed"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "bd init failed (exit 7)" in combined, combined


def test_init_passes_non_interactive_to_bd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ortus-btt3: `bd init` must be invoked with `--non-interactive` so it
    never blocks on hidden stdin prompts when ortus is run from a TTY.
    """
    import ortus.commands.init as init_mod

    captured: dict[str, list[str]] = {}

    def fake_run(args, cwd, check):  # noqa: ARG001 — match subprocess.run signature
        # Only the first call (bd init) is under test; init also shells out to
        # `bd remember` afterwards.
        captured.setdefault("args", list(args))
        # Pretend bd init succeeded so the rest of init can proceed.
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(init_mod.subprocess, "run", fake_run)
    target = tmp_path / "nonint"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["args"][:3] == ["bd", "init", "--non-interactive"], captured["args"]


def test_init_completes_with_closed_stdin(tmp_path: Path) -> None:
    """ortus-btt3: end-to-end check that `ortus init` completes promptly when
    invoked via a real subprocess with stdin=/dev/null (proxy for a terminal
    operator who never types anything). Regression guard for the bd-init prompt
    hang. Budget: 10s.
    """
    if shutil.which("ortus") is None:
        pytest.skip("ortus binary not on PATH")
    target = tmp_path / "closedstdin"
    t0 = time.monotonic()
    proc = subprocess.run(
        # `--codegraph off` keeps this about stdin: the monkeypatched bootstrap
        # seam does not reach a real subprocess, and indexing is not on trial.
        ["ortus", "init", str(target), "--codegraph", "off"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert elapsed < 10.0, f"ortus init took {elapsed:.2f}s with closed stdin (budget 10s)"
    assert (target / ".beads").is_dir()


def test_codegraph_bootstrap_indexes_pins_and_ignores(tmp_path: Path) -> None:
    """AC-2: init builds the index, pins the policy, ignores the dir."""
    target = tmp_path / "graphed"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (target / ".codegraph").is_dir()
    assert 'codegraph = "required"' in (target / ".ortusrc").read_text()
    assert ".codegraph/" in (target / ".gitignore").read_text()


def test_codegraph_bootstrap_skips_an_existing_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--force` over a repo that already has an index must not re-index."""
    import ortus.commands.init as init_mod

    target = tmp_path / "reindex"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    calls: list[Path] = []
    monkeypatch.setattr(init_mod, "_codegraph_index", lambda repo, **kw: calls.append(repo))
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == []


def test_codegraph_index_failure_fails_init_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero `codegraph init` leaves no half-written Ortus config."""
    import ortus.commands.init as init_mod

    def _boom(repo: Path, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=3, cmd=["codegraph", "init"])

    monkeypatch.setattr(init_mod, "_codegraph_index", _boom)
    target = tmp_path / "badindex"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert "codegraph init failed (exit 3)" in (result.stdout + result.stderr)
    assert not (target / ".ortusrc").exists()


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
