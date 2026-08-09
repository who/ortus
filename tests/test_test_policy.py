"""Regression tests for the phase-aware pytest policy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from tests import conftest
from tests.conftest import BdWorkspace, ci_gate_command, ci_gate_flags


REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = REPO_ROOT / "src" / "ortus" / "prompts" / "grind-prompt.md"
TESTING_GUIDE = REPO_ROOT / "docs" / "testing.md"


def _collect(marker: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/test_smoke_local.py",
            "-m",
            marker,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode in (0, 5), result.stderr
    return result.stdout


def test_live_provider_selection_is_explicit_and_excludes_fast_gate() -> None:
    live = _collect("live_provider")
    assert "test_plan_decompose_tiny_prd" in live
    assert "test_grind_one_task" in live
    assert "test_uv_build_produces_dynamic_version" not in live

    fast = _collect("fast")
    assert "test_plan_decompose_tiny_prd" not in fast
    assert "test_grind_one_task" not in fast
    assert "test_uv_build_produces_dynamic_version" not in fast


def test_network_build_selection_is_separate_from_live_provider() -> None:
    network = _collect("network")
    assert "test_uv_build_produces_dynamic_version" in network
    assert "test_plan_decompose_tiny_prd" not in network
    assert "test_grind_one_task" not in network


# ---------------------------------------------------------------------------
# Verification reproduces CI conditions, not developer conditions (ortus-q3lh).
# ---------------------------------------------------------------------------


def test_ambient_git_identity_neutralized(tmp_path: Path) -> None:
    """No test may inherit the operator's global git identity.

    This test never requests the fixture, so seeing an empty global config here
    is the evidence that it is autouse. A fixture that shells out to `git
    commit` must configure its own identity, the way a bare CI runner forces.
    """
    config = os.environ.get("GIT_CONFIG_GLOBAL")
    assert config, "the autouse fixture must point GIT_CONFIG_GLOBAL at a file"
    assert Path(config).read_text(encoding="utf-8") == ""

    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    def _git(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    assert _git("config", "--global", "--list").stdout.strip() == ""
    assert _git("config", "--global", "--get", "user.email").returncode != 0

    # A fixture that configures its own identity must keep working.
    repo = tmp_path / "self-configured"
    repo.mkdir()
    assert _git("init", "-q", ".", cwd=repo).returncode == 0
    assert _git("config", "user.email", "test@example.com", cwd=repo).returncode == 0
    assert _git("config", "user.name", "Test", cwd=repo).returncode == 0
    committed = _git("commit", "-q", "--allow-empty", "-m", "seed", cwd=repo)
    assert committed.returncode == 0, committed.stderr


_BUDGET_PROBE = '''
import time

import pytest


def test_unmarked_hermetic_test_over_budget() -> None:
    time.sleep(5.2)


@pytest.mark.slow
def test_slow_marked_test_over_budget() -> None:
    time.sleep(5.2)
'''


@pytest.mark.slow
def test_over_budget_is_rejected_under_verification_flags(tmp_path: Path) -> None:
    """The verification flags must reject an unmarked test over the budget.

    Two probes run in one inner pytest session under the flags a verifier now
    uses: the unmarked one must be rejected for its duration, and the
    `slow`-marked one must stay exempt exactly as it is under CI. Marked slow
    itself because proving a five-second breach costs five real seconds twice.
    """
    probe = tmp_path / "test_budget_probe.py"
    probe.write_text(_BUDGET_PROBE, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            # The probe lives outside `tests/`, so load the budget hook and its
            # option explicitly rather than relying on conftest discovery.
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            str(probe),
            *ci_gate_flags(),
            "-q",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "1 failed" in combined and "1 passed" in combined, combined

    rejected = [
        line
        for line in combined.splitlines()
        if "must be optimized or marked slow" in line
    ]
    assert rejected, combined
    assert all("test_unmarked_hermetic_test_over_budget" in line for line in rejected), (
        f"the budget rejected something other than the unmarked probe: {rejected}"
    )


def test_testing_guide_documents_flag_parity() -> None:
    """Packet authors and verifiers must read the same parity contract."""
    guide = TESTING_GUIDE.read_text(encoding="utf-8")
    assert "Verification-to-CI flag parity" in guide
    for flag in ci_gate_flags():
        assert flag in guide, f"docs/testing.md does not name the CI gate flag {flag!r}"
    assert "never which tests are selected" in guide
    assert "GIT_CONFIG_GLOBAL" in guide


def test_worker_guidance_uses_bounded_hermetic_default() -> None:
    prompt = PROMPT.read_text(encoding="utf-8")
    command = "uv run pytest -m fast -n auto --test-timeout=30"
    assert command in prompt
    assert "Never run `network` or `live_provider` by default" in prompt
    assert "full local `uv run pytest`" not in prompt


# ---------------------------------------------------------------------------
# Workers parallelise, CI does not (ortus-3ehq).
# ---------------------------------------------------------------------------


def test_ci_gate_stays_serial() -> None:
    """AC-3: the comprehensive gate must keep running one test at a time.

    The duration budget is a claim about a test on a quiet machine. Adding
    xdist workers to this command would make every measured duration a function
    of how many other tests happened to be running, which is why parallelism
    lives in the worker and verifier commands instead of here (and never in
    `addopts`, which this command inherits).
    """
    command = ci_gate_command()
    assert "--enforce-duration-budget" in command, command
    assert not re.search(r"(?:^|\s)(?:-n|--numprocesses)(?:[=\s]|$)", command), (
        f"the CI gate acquired parallel workers, so its duration budget now "
        f"measures contention rather than the test: {command!r}"
    )

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    addopts = re.search(r'^addopts = "(.*)"$', pyproject, re.MULTILINE)
    assert addopts is not None, "pyproject.toml no longer declares pytest addopts"
    assert not re.search(
        r"(?:^|\s)(?:-n|--numprocesses)(?:[=\s]|$)", addopts.group(1)
    ), (
        f"shared addopts must not force parallelism onto the CI gate: "
        f"{addopts.group(1)!r}"
    )


def test_parallel_sweeps_have_their_dependency() -> None:
    """The guidance tells both phases to pass `-n auto`; it must resolve."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "pytest-xdist" in pyproject, (
        "worker and verifier guidance passes `-n auto`, so pytest-xdist has to "
        "be a dev dependency"
    )


def test_testing_guide_documents_parallel_split() -> None:
    """AC-4: the guide records the split and why the two cannot be combined."""
    guide = TESTING_GUIDE.read_text(encoding="utf-8")

    assert "-n auto" in guide
    assert "pytest-xdist" in guide
    # The split itself: parallel for workers, serial-with-budget for CI.
    assert "single-threaded" in guide
    for flag in ci_gate_flags():
        assert flag in guide, f"docs/testing.md does not name the CI gate flag {flag!r}"
    # The reason, not just the rule — an editor who only reads the rule will
    # eventually "simplify" it by putting `-n auto` in addopts.
    assert "mutually exclusive" in guide
    assert "quiet machine" in guide


# ---------------------------------------------------------------------------
# One `bd init` per session, then copies (ortus-apmf).
# ---------------------------------------------------------------------------

# The grind-family modules whose workspace builders must not pay for `bd init`.
_MIGRATED_MODULES = (
    "tests/conftest.py",
    "tests/test_grind.py",
    "tests/test_grind_corrections.py",
    "tests/test_grind_finalization.py",
    "tests/test_grind_orphan_policy.py",
    "tests/test_grind_state_delta.py",
    "tests/test_grind_worker_timeout.py",
)


def test_template_workspace_initialized_once(
    bd_template_bare: BdWorkspace, bd_template_leaf: BdWorkspace
) -> None:
    """AC-1: two template kinds, still exactly one `bd init` for the session.

    The seeded kinds are built by copying the bare template, so asking for a
    second kind must not buy a second init — that is the whole saving.
    """
    assert conftest.bd_init_calls() == 1, (
        f"expected one `bd init` per session; saw {conftest.bd_init_calls()}"
    )
    assert bd_template_bare.issues == ()
    assert len(bd_template_leaf.issues) == 1
    assert bd_template_leaf.path != bd_template_bare.path


def test_copied_workspace_is_usable(
    tmp_path: Path, bd_workspace: Callable[..., BdWorkspace]
) -> None:
    """AC-3: a copy lists the baked issues and mints new ids of its own.

    A relocated Dolt database that opened read-only, or one still pointed at
    the template, would fail exactly here rather than as a confusing failure
    inside some grind test that assumed its workspace worked.
    """
    workspace = conftest.copy_bd_workspace(tmp_path / "copied", "leaf")

    listed = json.loads(
        subprocess.run(
            ["bd", "list", "--all", "--json"],
            cwd=workspace.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        or "[]"
    )
    assert {issue["id"] for issue in listed} == set(workspace.issues)

    minted = subprocess.run(
        ["bd", "create", "--silent", "--type", "task", "--title", "in the copy"],
        cwd=workspace.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert minted and minted not in workspace.issues

    # The copy must stand alone: git's hooksPath is the one absolute path a bd
    # workspace carries, and a copy still aimed at the template would run the
    # template's hooks and write through to it.
    hooks_path = subprocess.run(
        ["git", "config", "core.hooksPath"],
        cwd=workspace.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(hooks_path) == workspace.path / ".beads" / "hooks"

    # ...and the template it came from is untouched by all of the above, so a
    # second copy — here via the fixture form — starts from the same state.
    second = bd_workspace("second", "leaf")
    assert second.issues == workspace.issues
    assert conftest.bd_template("leaf").issues == workspace.issues


def test_grind_fixtures_copy_a_template_instead_of_bd_init() -> None:
    """AC-2: only the template builder may run `bd init` (ortus-apmf).

    `bd init` costs ~1.6s against ~25ms for a copy, so a builder that quietly
    reintroduces one puts its whole module back on the setup floor this issue
    removed. Asserted over the source rather than left to a one-off grep.
    """
    offenders: dict[str, int] = {}
    for name in _MIGRATED_MODULES:
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        count = len(re.findall(r'"bd",\s*"init"|"init",\s*"--non-interactive"', source))
        if count:
            offenders[name] = count

    # conftest's `_build_bare` is the one fixture that exists to run `bd init`.
    assert offenders == {"tests/conftest.py": 1}, (
        f"grind-family workspace builders must copy a template: {offenders}"
    )


def test_testing_guide_documents_template_pattern() -> None:
    """AC-5: a new test's author has to be able to find the pattern."""
    guide = TESTING_GUIDE.read_text(encoding="utf-8")
    assert "copy_bd_workspace" in guide
    assert "bd_workspace" in guide
    # The rule and its reason, so the next author copies rather than inits.
    assert "never write into one" in guide
    assert "bd init" in guide
