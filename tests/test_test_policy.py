"""Regression tests for the phase-aware pytest policy."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = REPO_ROOT / "src" / "ortus" / "prompts" / "grind-prompt.md"


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


def test_worker_guidance_uses_bounded_hermetic_default() -> None:
    prompt = PROMPT.read_text(encoding="utf-8")
    command = "uv run pytest -m fast --test-timeout=30 --enforce-duration-budget"
    assert command in prompt
    assert "Never run `network` or `live_provider` by default" in prompt
    assert "full local `uv run pytest`" not in prompt
