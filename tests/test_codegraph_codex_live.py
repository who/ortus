"""Opt-in proof that a real fresh Codex process can query CodeGraph."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ortus.core.agent import CodexRunner
from ortus.core.codegraph import (
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    parse_transcript,
)


@pytest.mark.live_provider
@pytest.mark.slow
def test_real_codex_worker_completes_bounded_codegraph_query(tmp_path: Path) -> None:
    if os.environ.get("ORTUS_RUN_CODEGRAPH_CODEX_SMOKE") != "1":
        pytest.skip("set ORTUS_RUN_CODEGRAPH_CODEX_SMOKE=1 to run")
    repo = Path.cwd()
    probe = CodeGraphAdapter().probe(repo, CodeGraphMode.REQUIRED, backend="codex")
    runner = CodexRunner(codegraph=probe.capability, sandbox_mode="read-only")
    log = tmp_path / "codex-codegraph.jsonl"
    rc = runner.run(
        "Call codegraph_explore exactly once with the bounded query "
        "'Orient to src/ortus/core/agent.py'. Do not call shell tools. Then stop.",
        repo=repo,
        log_path=log,
        timeout=180,
    )
    assert rc == 0
    summary = parse_transcript(
        log, phase=CodeGraphPhase.VERIFICATION, probe=probe
    )
    assert summary.capability_observed
