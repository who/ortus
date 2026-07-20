"""Hermetic tests for the pre-edit Codex CodeGraph handshake gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ortus.commands.grind import _codex_codegraph_handshake
from ortus.core.codegraph import (
    CodeGraphCapability,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    CodeGraphUnavailable,
)
from ortus.core.profiles import AgentProfile, Phase


def _probe(mode: CodeGraphMode) -> CodeGraphProbe:
    return CodeGraphProbe(
        mode,
        True,
        True,
        True,
        capability=CodeGraphCapability("/bin/codegraph"),
    )


class _HandshakeRunner:
    def run_codegraph_handshake(
        self, *, phase: str, log_path: Path, **kwargs: object
    ) -> int:
        with log_path.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": phase,
                            "type": "mcp_tool_call",
                            "server": "codegraph",
                            "tool": "codegraph_explore",
                            "arguments": {"query": f"{phase} orientation"},
                            "result": {"results": [{"symbol": "ok"}]},
                        },
                    }
                )
                + "\n"
            )
        return 0


@pytest.mark.parametrize(
    ("phase", "profile_phase"),
    [
        (CodeGraphPhase.IMPLEMENTATION, Phase.IMPLEMENT),
        (CodeGraphPhase.VERIFICATION, Phase.VERIFY),
    ],
)
def test_codex_handshake_succeeds_for_both_fresh_worker_postures(
    tmp_path: Path, phase: CodeGraphPhase, profile_phase: Phase
) -> None:
    log = tmp_path / "grind.log"
    result = _codex_codegraph_handshake(
        _HandshakeRunner(),  # type: ignore[arg-type]
        repo=tmp_path,
        log_path=log,
        phase=phase,
        probe=_probe(CodeGraphMode.REQUIRED),
        profile=AgentProfile("codex", profile_phase),
        timeout=10,
    )
    assert result.available
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(record.get("kind") == "handshake" and record["success"] for record in records)
    assert any(record.get("kind") == "query" for record in records)


def test_auto_child_missing_records_precise_fallback(tmp_path: Path) -> None:
    result = _codex_codegraph_handshake(
        object(),  # type: ignore[arg-type]
        repo=tmp_path,
        log_path=tmp_path / "grind.log",
        phase=CodeGraphPhase.IMPLEMENTATION,
        probe=_probe(CodeGraphMode.AUTO),
        profile=AgentProfile("codex", Phase.IMPLEMENT),
        timeout=10,
    )
    assert not result.available
    assert result.reason == "Codex runner does not support the CodeGraph child handshake"


def test_required_child_missing_halts_at_handshake_gate(tmp_path: Path) -> None:
    with pytest.raises(CodeGraphUnavailable, match="runner does not support"):
        _codex_codegraph_handshake(
            object(),  # type: ignore[arg-type]
            repo=tmp_path,
            log_path=tmp_path / "grind.log",
            phase=CodeGraphPhase.IMPLEMENTATION,
            probe=_probe(CodeGraphMode.REQUIRED),
            profile=AgentProfile("codex", Phase.IMPLEMENT),
            timeout=10,
        )
