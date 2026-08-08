"""Regression tests for the phase-aware pytest policy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import ci_gate_flags


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
    command = "uv run pytest -m fast --test-timeout=30 --enforce-duration-budget"
    assert command in prompt
    assert "Never run `network` or `live_provider` by default" in prompt
    assert "full local `uv run pytest`" not in prompt
