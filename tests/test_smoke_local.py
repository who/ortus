"""Hermetic end-to-end smoke harness against the local-dev Python ortus build
(ortus-inam).

Distinct from `test_e2e_smoke.py` (which uses Typer's in-process `CliRunner`),
this module shells out to the *built* ortus binary via `uv run --project <repo>`
so the harness always exercises whatever is at HEAD of this branch — wheel
metadata, entry-point shim, sandbox spawn surface, the lot.

Fast tests run by default and stay under ~30s wall-clock total. Slow tests
(`@pytest.mark.slow`) exercise verbs that need a real `claude` and are skipped
unless `./scripts/smoke-local.sh --slow` (or `pytest --slow`) is passed.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

pytestmark = pytest.mark.smoke

# ---------------------------------------------------------------------------
# Resolve the repo root once; the harness must invoke the local-dev build, not
# whatever happens to be on PATH. `uv run --project <root>` is the modern
# uv-native pattern and matches the toolchain locked into pyproject.toml.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
)


OrtusCallable = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture()
def local_ortus() -> OrtusCallable:
    """Return a callable that invokes the local-dev ortus binary.

    Usage:
        proc = local_ortus("init", str(tmp_repo))
        proc = local_ortus("check", str(tmp_repo), check=False)
        proc = local_ortus("human", str(tmp_repo), env={...}, cwd=str(tmp_repo))
    """

    def _run(
        *args: str,
        check: bool = False,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["uv", "run", "--project", str(_REPO_ROOT), "ortus", *args]
        return subprocess.run(
            cmd,
            check=check,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
        )

    return _run


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Hermetic project root for `ortus init`. Auto-cleaned by tmp_path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# ---------------------------------------------------------------------------
# Skip-helpers: many checks need third-party binaries (bd, claude, jq, bwrap).
# Rather than guard every test by hand, surface clear skip reasons up front.
# ---------------------------------------------------------------------------


def _require(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        pytest.skip(f"required binaries not on PATH: {', '.join(missing)}")


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute $HOME so the user's real ~/.claude/settings.json can't influence
    the test outcome — `ortus check`'s hook-precheck reads it."""
    fake_home = tmp_path_factory.mktemp("fake-home")
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


# ---------------------------------------------------------------------------
# Sanity: confirm the harness really is hitting the local-dev build.
# ---------------------------------------------------------------------------


def test_local_ortus_version_matches_pyproject(local_ortus: OrtusCallable) -> None:
    proc = local_ortus("--version", check=True)
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    # Cheap and stable: scan for `version = "X"` in [project] block.
    version_line = next(
        line for line in pyproject.splitlines() if line.strip().startswith("version =")
    )
    expected = version_line.split("=", 1)[1].strip().strip('"').strip("'")
    assert expected in proc.stdout, (
        f"local ortus reports {proc.stdout!r}; expected version {expected!r} "
        f"from pyproject.toml — harness may be hitting a stale binary"
    )


# ---------------------------------------------------------------------------
# init verb
# ---------------------------------------------------------------------------


def test_init_creates_expected_files(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #6: init creates .beads/, .claude/settings.json, .ortusrc, AGENTS.md."""
    _require("bd")
    proc = local_ortus("init", str(tmp_repo))
    assert proc.returncode == 0, (
        f"ortus init exited {proc.returncode}.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    expected = [
        tmp_repo / ".beads",
        tmp_repo / ".claude" / "settings.json",
        tmp_repo / ".ortusrc",
        tmp_repo / "AGENTS.md",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    assert not missing, (
        f"ortus init did not create expected artifacts: {missing}. "
        f"The init verb's create-on-init logic may have regressed."
    )


def test_init_settings_shape(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #6: settings.json declares bd in sandbox.excludedCommands and does
    not silently turn off hooks."""
    _require("bd")
    local_ortus("init", str(tmp_repo), check=True)
    data = json.loads((tmp_repo / ".claude" / "settings.json").read_text())
    excluded = data.get("sandbox", {}).get("excludedCommands", [])
    assert "bd" in excluded, (
        f".claude/settings.json sandbox.excludedCommands is {excluded!r}; "
        f"expected 'bd' present. ortus init's settings rendering may have drifted."
    )
    assert "bd *" in excluded, (
        f".claude/settings.json sandbox.excludedCommands is {excluded!r}; "
        f"expected 'bd *' present (covers `bd <subcmd>` invocations)."
    )
    assert not data.get("disableAllHooks", False), (
        ".claude/settings.json sets disableAllHooks=true — ortus init must "
        "never ship a disabled-hooks template (would break bd prime SessionStart)."
    )


def test_init_idempotency_force(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #6: re-running init without --force is refused; --force succeeds."""
    _require("bd")
    local_ortus("init", str(tmp_repo), check=True)
    again = local_ortus("init", str(tmp_repo))
    assert again.returncode != 0, (
        "second `ortus init` without --force exited 0; expected refusal "
        "since .beads/ already exists (would silently clobber state)."
    )
    forced = local_ortus("init", str(tmp_repo), "--force")
    assert forced.returncode == 0, (
        f"`ortus init --force` exited {forced.returncode}.\n"
        f"stdout:\n{forced.stdout}\nstderr:\n{forced.stderr}"
    )


# ---------------------------------------------------------------------------
# check verb
# ---------------------------------------------------------------------------


def test_check_green_path(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #6: check on a healthy just-init'd repo with all binaries present.

    Skips if any of {bd, claude, jq, bwrap} is missing — those are check's
    own external prereqs and their absence is not a smoke-harness failure.
    """
    _require("bd", "claude", "jq", "bwrap")
    local_ortus("init", str(tmp_repo), check=True)
    proc = local_ortus("check", str(tmp_repo))
    assert proc.returncode == 0, (
        f"`ortus check` exited {proc.returncode} on a fresh init'd repo with "
        f"all prereq binaries present. Some downstream check has regressed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "PASS" in proc.stdout, (
        f"`ortus check` output did not contain 'PASS'; the table rendering "
        f"may have changed.\nstdout:\n{proc.stdout}"
    )


def test_check_disabled_hooks_fails(
    local_ortus: OrtusCallable, tmp_repo: Path
) -> None:
    """AC #6: disableAllHooks=true → check exits 1 with hook-precheck error."""
    _require("bd")
    local_ortus("init", str(tmp_repo), check=True)
    settings = tmp_repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["disableAllHooks"] = True
    settings.write_text(json.dumps(data))
    proc = local_ortus("check", str(tmp_repo))
    assert proc.returncode == 1, (
        f"`ortus check` with disableAllHooks=true exited {proc.returncode}; "
        f"expected 1. hooks precheck may have regressed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "hooks" in combined.lower(), (
        f"`ortus check` did not mention 'hooks' in its failure output; the "
        f"diagnostic may have lost its actionable cue.\noutput:\n{combined}"
    )


def test_check_missing_bd_fails(
    local_ortus: OrtusCallable, tmp_repo: Path, tmp_path: Path
) -> None:
    """AC #6: with bd masked from PATH, check exits non-zero and flags bd."""
    _require("bd")
    local_ortus("init", str(tmp_repo), check=True)
    # Build a PATH stub that contains *only* uv + git + jq + bwrap + claude —
    # explicitly NOT bd. The subprocess will then see bd as missing.
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    for needed in ("uv", "git", "jq", "bwrap", "claude", "sh", "bash", "env"):
        src = shutil.which(needed)
        if src is None:
            continue
        os.symlink(src, stub / needed)
    env = {
        **os.environ,
        "PATH": str(stub),
    }
    proc = local_ortus("check", str(tmp_repo), env=env)
    assert proc.returncode != 0, (
        f"`ortus check` with bd masked from PATH exited 0; expected non-zero. "
        f"bd-presence check may have regressed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "bd" in combined, (
        f"`ortus check` did not mention 'bd' in its failure output; the "
        f"diagnostic may have lost its actionable cue.\noutput:\n{combined}"
    )


# ---------------------------------------------------------------------------
# tail verb (no external binaries required)
# ---------------------------------------------------------------------------


def test_tail_runs_against_seeded_log(tmp_repo: Path) -> None:
    """AC #6: tail runs without crashing and surfaces content from a seeded
    grind-*.log fixture.

    `ortus tail` polls forever (no --iterations on the CLI surface), so we
    drive it via Popen and SIGTERM after the first read, then verify the
    accumulated stdout. The verb flushes after every line, so partial
    output survives the kill.
    """
    _require("bd")
    # init in-process via the same subprocess pattern as local_ortus.
    init_proc = subprocess.run(
        ["uv", "run", "--project", str(_REPO_ROOT), "ortus", "init", str(tmp_repo)],
        check=True, capture_output=True, text=True,
    )
    assert init_proc.returncode == 0
    logs = tmp_repo / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "grind-smoke.log").write_text(
        '{"type":"assistant","message":{"content":"smoke harness saw this"}}\n'
    )
    proc = subprocess.Popen(
        ["uv", "run", "--project", str(_REPO_ROOT), "ortus", "tail", str(tmp_repo)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=3.0)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
    assert "smoke harness saw this" in stdout, (
        f"`ortus tail` did not surface seeded log content within 3s; the "
        f"log-filter or flush may have regressed.\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )


# ---------------------------------------------------------------------------
# human verb
# ---------------------------------------------------------------------------


def test_human_writes_todo(
    local_ortus: OrtusCallable, tmp_repo: Path
) -> None:
    """AC #6: human emits HUMAN-TODO.md listing each flagged issue."""
    _require("bd")
    local_ortus("init", str(tmp_repo), check=True)
    issue_id = subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "smoke decision needed",
            "--type", "task", "--priority", "2", "--labels", "human",
        ],
        cwd=str(tmp_repo), check=True, capture_output=True, text=True,
    ).stdout.strip()
    proc = local_ortus("human", str(tmp_repo))
    assert proc.returncode == 0, (
        f"`ortus human` exited {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    todo = tmp_repo / "HUMAN-TODO.md"
    assert todo.is_file(), (
        f"`ortus human` did not write HUMAN-TODO.md at {todo}. The file-emit "
        f"path may have regressed."
    )
    body = todo.read_text()
    assert issue_id in body, (
        f"HUMAN-TODO.md does not mention the seeded issue {issue_id!r}; "
        f"the human-flag selection logic may have regressed.\nbody:\n{body}"
    )
    assert "smoke decision needed" in body, (
        f"HUMAN-TODO.md does not contain the seeded title; rendering may "
        f"have regressed.\nbody:\n{body}"
    )


# ---------------------------------------------------------------------------
# triage verb — interactive, skipped per AC #8
# ---------------------------------------------------------------------------


def test_triage_skipped_interactive() -> None:
    pytest.skip(
        "interactive; AskUserQuestion can't be mocked headless. "
        "Covered by tests/test_triage.py with monkeypatched prompts."
    )


# ---------------------------------------------------------------------------
# Slow tests — gated behind `@pytest.mark.slow`. Run via `--slow`.
# These spend real claude API budget.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_plan_decompose_tiny_prd(
    local_ortus: OrtusCallable, tmp_repo: Path
) -> None:
    """AC #7: plan decomposes a synthetic 3-task PRD into bd issues."""
    _require("bd", "claude")
    local_ortus("init", str(tmp_repo), check=True)
    prd = tmp_repo / "tiny.prd.md"
    prd.write_text(
        "# Tiny PRD\n\n"
        "Build a calculator with exactly three tasks:\n"
        "1. Implement add(a, b)\n"
        "2. Implement subtract(a, b)\n"
        "3. Write unit tests for both\n"
    )
    proc = local_ortus("plan", str(tmp_repo), str(prd), timeout=300.0)
    assert proc.returncode == 0, (
        f"`ortus plan` exited {proc.returncode} on tiny PRD.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    issues = json.loads(
        subprocess.run(
            ["bd", "list", "--status", "open", "--json"],
            cwd=str(tmp_repo), check=True, capture_output=True, text=True,
        ).stdout
    )
    assert len(issues) >= 3, (
        f"`ortus plan` produced {len(issues)} issues; expected ≥ 3 from a "
        f"3-task PRD. Decomposition logic may have regressed."
    )


@pytest.mark.slow
def test_grind_one_task(local_ortus: OrtusCallable, tmp_repo: Path) -> None:
    """AC #7: grind --tasks 1 against a seeded fixture closes one issue."""
    _require("bd", "claude")
    local_ortus("init", str(tmp_repo), check=True)
    subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "Write hello-world to README.md",
            "--description", "Append the line 'hello world' to README.md.",
            "--type", "task", "--priority", "2",
        ],
        cwd=str(tmp_repo), check=True, capture_output=True, text=True,
    )
    open_before = json.loads(
        subprocess.run(
            ["bd", "list", "--status", "open", "--json"],
            cwd=str(tmp_repo), check=True, capture_output=True, text=True,
        ).stdout
    )
    proc = local_ortus("grind", str(tmp_repo), "--tasks", "1", timeout=600.0)
    assert proc.returncode == 0, (
        f"`ortus grind` exited {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    open_after = json.loads(
        subprocess.run(
            ["bd", "list", "--status", "open", "--json"],
            cwd=str(tmp_repo), check=True, capture_output=True, text=True,
        ).stdout
    )
    assert len(open_after) == len(open_before) - 1, (
        f"`ortus grind --tasks 1` did not close exactly one issue: "
        f"before={len(open_before)} after={len(open_after)}. grind loop may "
        f"have regressed."
    )
