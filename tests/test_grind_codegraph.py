"""Hermetic tests for the pre-edit Codex CodeGraph handshake gate.

Child handshake (`_codex_codegraph_handshake`) and the outer probe abort stay
on the live grind path. The post-worker `require_handshake(implementation_summary)`
gate is gone: f2he.2 judges bd status and continues, so a silent worker under
required does not halt on a missing implementation MCP event. The worker-owned
prompt is the implementation handshake.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.grind import _codex_codegraph_handshake
from ortus.core.codegraph import (
    CodeGraphCapability,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    CodeGraphUnavailable,
)
from ortus.core.profiles import AgentProfile, Phase

runner = CliRunner()
_GRIND_PY = Path(__file__).resolve().parents[1] / "src" / "ortus" / "commands" / "grind.py"


def _probe(mode: CodeGraphMode) -> CodeGraphProbe:
    return CodeGraphProbe(
        mode,
        True,
        True,
        True,
        capability=CodeGraphCapability("/bin/codegraph"),
    )


def _emit_query(log_path: Path, phase: str) -> None:
    """One successful `codegraph_explore` call in the agent's JSONL stream."""
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


class _HandshakeRunner:
    def run_codegraph_handshake(
        self, *, phase: str, log_path: Path, **kwargs: object
    ) -> int:
        _emit_query(log_path, phase)
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
    # The healthy probe/succeeded lines narrate to this same log as plain
    # timestamped text (ortus-kawu), alongside the structured records.
    records = [
        json.loads(line)
        for line in log.read_text().splitlines()
        if line.startswith("{")
    ]
    assert any(record.get("kind") == "handshake" and record["success"] for record in records)
    assert any(record.get("kind") == "query" for record in records)
    assert f"{phase.value} CodeGraph child handshake succeeded" in log.read_text()


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


@pytest.mark.codegraph_default
@pytest.mark.parametrize("verb", ["grind", "plan"])
def test_default_mode_required_aborts_at_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """AC-8: no `--codegraph` flag and no config key means `required`.

    The repo has a bd workspace but no `.codegraph/`, so both verbs must stop
    at the probe with the remediation text rather than launching an agent.
    """
    repo = tmp_path / "unindexed"
    (repo / ".beads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    args = [verb, str(repo)] + (["--dry-run"] if verb == "grind" else [])
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    compact = "".join(combined.split())
    assert "CodeGraphrequiredbutunavailable" in compact, combined
    assert "codegraphinit" in compact, combined


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


def test_post_worker_implementation_handshake_block_is_gone() -> None:
    """AC-1: the dead require_handshake(implementation_summary) gate is gone.

    f2he.2 continues on judged bd status before that call could run. The live
    gates are the outer probe and the Codex child handshake; the worker-owned
    prompt is the implementation handshake.
    """
    tree = ast.parse(_GRIND_PY.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if name != "require_handshake" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name) and arg.id == "implementation_summary":
            hits.append(node.lineno)
    assert hits == [], (
        "post-worker require_handshake(implementation_summary) must stay gone; "
        f"found at lines {hits}"
    )
    source = _GRIND_PY.read_text(encoding="utf-8")
    assert "handshake-not-required" not in source
    assert "implementation CodeGraph handshake not required" not in source
    assert "_codex_codegraph_handshake" in source
