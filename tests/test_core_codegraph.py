from __future__ import annotations

import json
from pathlib import Path

import pytest

from ortus.core.codegraph import (
    MAX_LABEL,
    MCP_ORIENT_QUERY,
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    CodeGraphRpcError,
    CodeGraphUnavailable,
    append_normalized,
    parse_transcript,
    require_handshake,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _available(mode: CodeGraphMode = CodeGraphMode.AUTO) -> CodeGraphProbe:
    return CodeGraphProbe(mode, True, True, True)


def test_probe_modes_and_required_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = CodeGraphAdapter()
    off = adapter.probe(tmp_path, CodeGraphMode.OFF)
    assert not off.available and off.reason == "disabled by policy"
    auto = adapter.probe(tmp_path, CodeGraphMode.AUTO)
    assert not auto.available and ".codegraph" in (auto.reason or "")
    with pytest.raises(CodeGraphUnavailable, match="codegraph init"):
        adapter.probe(tmp_path, CodeGraphMode.REQUIRED)


def test_codex_probe_produces_the_child_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".codegraph").mkdir()
    monkeypatch.setattr("ortus.core.codegraph.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        CodeGraphAdapter, "mcp_tools_call", lambda self, *a, **k: {"content": []}
    )
    probe = CodeGraphAdapter().probe(tmp_path, CodeGraphMode.AUTO, backend="codex")
    assert probe.available
    assert probe.capability is not None
    assert probe.capability.command == "/bin/codegraph"
    assert probe.capability.args == ("serve", "--mcp")


def test_grok_probe_is_cli_and_index_not_injected_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".codegraph").mkdir()
    monkeypatch.setattr("ortus.core.codegraph.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        CodeGraphAdapter, "mcp_tools_call", lambda self, *a, **k: {"content": []}
    )
    probe = CodeGraphAdapter().probe(tmp_path, CodeGraphMode.AUTO, backend="grok")
    assert probe.available
    assert probe.capability is None
    assert probe.cli_present and probe.index_present


def test_codex_probe_reports_missing_server_with_initialized_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".codegraph").mkdir()
    monkeypatch.setattr("ortus.core.codegraph.shutil.which", lambda name: None)
    probe = CodeGraphAdapter().probe(tmp_path, CodeGraphMode.AUTO, backend="codex")
    assert not probe.available and probe.capability is None
    assert probe.reason == "codegraph CLI is not on PATH"


def test_parent_available_child_missing_mismatch_is_not_reported_as_engaged(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "child.jsonl"
    transcript.write_text('{"type":"turn.completed"}\n')
    summary = parse_transcript(
        transcript, phase=CodeGraphPhase.IMPLEMENTATION, probe=_available()
    )
    assert summary.probe.available and not summary.capability_observed
    assert "availability: unavailable" in summary.report()
    journal = tmp_path / "journal.jsonl"
    append_normalized(journal, summary)
    record = json.loads(journal.read_text().splitlines()[0])
    assert record["available"] is False
    assert record["prerequisites_ready"] is True


def test_claude_normalization_hit_miss_error_truncation_and_redaction(tmp_path: Path) -> None:
    summary = parse_transcript(
        FIXTURES / "codegraph-claude-events.jsonl",
        phase=CodeGraphPhase.VERIFICATION,
        probe=_available(),
    )
    assert [event.hit for event in summary.events] == [True, False, None]
    assert [event.success for event in summary.events] == [True, True, False]
    assert summary.events[-1].truncated
    assert len(summary.events[-1].query) == MAX_LABEL
    journal = tmp_path / "journal.log"
    append_normalized(journal, summary)
    normalized = journal.read_text()
    assert "SECRET" not in normalized and "source" not in normalized
    assert all(json.loads(line)["type"] == "ortus.codegraph" for line in normalized.splitlines())


def test_grok_use_tool_mcp_counts_as_handshake(tmp_path: Path) -> None:
    transcript = tmp_path / "grok.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "tool_call",
                "toolCallId": "call-1",
                "toolName": "use_tool",
                "status": "pending",
                "rawInput": {
                    "tool_name": "codegraph__codegraph_explore",
                    "tool_input": {"query": "orient to this repository"},
                },
            }
        )
        + "\n"
        + json.dumps({"type": "tool_call_update", "toolCallId": "call-1"})
        + "\n"
        + json.dumps(
            {
                "type": "tool_call_update",
                "toolCallId": "call-1",
                "status": "completed",
                "rawOutput": {
                    "type": "MCP",
                    "tool_name": "codegraph_explore",
                    "server_name": "codegraph",
                    "output": {"text": "symbols found"},
                },
            }
        )
        + "\n"
    )
    summary = parse_transcript(
        transcript,
        phase=CodeGraphPhase.IMPLEMENTATION,
        probe=_available(CodeGraphMode.REQUIRED),
    )
    assert summary.capability_observed
    assert summary.events[0].success
    assert summary.events[0].query == "orient to this repository"
    require_handshake(summary)


def test_codex_normalization_success_and_empty_result() -> None:
    summary = parse_transcript(
        FIXTURES / "codegraph-codex-events.jsonl",
        phase=CodeGraphPhase.IMPLEMENTATION,
        probe=_available(),
    )
    assert len(summary.events) == 2
    assert summary.events[0].hit is True
    assert summary.events[1].hit is False


def test_query_failure_does_not_count_as_a_handshake(tmp_path: Path) -> None:
    transcript = tmp_path / "failed.jsonl"
    transcript.write_text(
        '{"type":"item.completed","item":{"id":"x","type":"mcp_tool_call",'
        '"server":"codegraph","tool":"codegraph_explore",'
        '"arguments":{"query":"orientation"},"error":"server unavailable"}}\n'
    )
    summary = parse_transcript(
        transcript,
        phase=CodeGraphPhase.IMPLEMENTATION,
        probe=_available(CodeGraphMode.REQUIRED),
    )
    assert not summary.capability_observed
    assert "agent CodeGraph queries all failed" in summary.fallbacks
    with pytest.raises(CodeGraphUnavailable):
        require_handshake(summary)


def test_unavailable_and_negative_required_handshake(tmp_path: Path) -> None:
    absent = CodeGraphProbe(CodeGraphMode.AUTO, False, False, False, "missing")
    summary = parse_transcript(
        tmp_path / "none", phase=CodeGraphPhase.PLANNING, probe=absent
    )
    assert summary.fallbacks == ["missing"]

    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"type":"turn.completed"}\n')
    required = parse_transcript(
        empty,
        phase=CodeGraphPhase.PLANNING,
        probe=_available(CodeGraphMode.REQUIRED),
    )
    with pytest.raises(CodeGraphUnavailable, match="capability"):
        require_handshake(required)


_FAKE_MCP = """#!/usr/bin/env python3
import json
import sys

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialize":
        sys.stdout.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": "2024-11-05"},
                }
            )
            + "\\n"
        )
        sys.stdout.flush()
    elif method == "tools/call":
        name = (message.get("params") or {}).get("name")
        if name != "codegraph_explore":
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"message": f"unknown tool {name}"},
            }
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }
        sys.stdout.write(json.dumps(reply) + "\\n")
        sys.stdout.flush()
        break
"""


_FAKE_MCP_ERROR = """#!/usr/bin/env python3
import json
import sys

for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "initialize":
        sys.stdout.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": "2024-11-05"},
                }
            )
            + "\\n"
        )
        sys.stdout.flush()
    elif message.get("method") == "tools/call":
        sys.stdout.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"message": "server unavailable"},
                }
            )
            + "\\n"
        )
        sys.stdout.flush()
        break
"""


def _write_exec(path: Path, source: str) -> Path:
    path.write_text(source)
    path.chmod(0o755)
    return path


def test_mcp_tools_call_returns_explore_result(tmp_path: Path) -> None:
    server = _write_exec(tmp_path / "fake-mcp", _FAKE_MCP)
    result = CodeGraphAdapter().mcp_tools_call(
        tmp_path, MCP_ORIENT_QUERY, command=str(server), args=()
    )
    assert result["content"][0]["text"] == "ok"


def test_mcp_tools_call_raises_on_rpc_error(tmp_path: Path) -> None:
    server = _write_exec(tmp_path / "fake-mcp-error", _FAKE_MCP_ERROR)
    with pytest.raises(CodeGraphRpcError, match="tools/call failed"):
        CodeGraphAdapter().mcp_tools_call(
            tmp_path, MCP_ORIENT_QUERY, command=str(server), args=()
        )


def test_required_probe_performs_mcp_tools_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".codegraph").mkdir()
    monkeypatch.setattr("ortus.core.codegraph.shutil.which", lambda name: f"/bin/{name}")
    calls: list[str] = []

    def _fake_mcp(self: CodeGraphAdapter, repo: Path, query: str, **kwargs: object) -> dict:
        calls.append(query)
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(CodeGraphAdapter, "mcp_tools_call", _fake_mcp)
    probe = CodeGraphAdapter().probe(tmp_path, CodeGraphMode.REQUIRED)
    assert probe.available
    assert calls == [MCP_ORIENT_QUERY]


def test_required_probe_mcp_tools_call_fails_when_rpc_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".codegraph").mkdir()
    monkeypatch.setattr("ortus.core.codegraph.shutil.which", lambda name: f"/bin/{name}")

    def _boom(self: CodeGraphAdapter, *args: object, **kwargs: object) -> dict:
        raise CodeGraphRpcError("server unavailable")

    monkeypatch.setattr(CodeGraphAdapter, "mcp_tools_call", _boom)
    with pytest.raises(CodeGraphUnavailable, match="MCP tools/call failed"):
        CodeGraphAdapter().probe(tmp_path, CodeGraphMode.REQUIRED)


def test_auto_probe_mcp_failure_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".codegraph").mkdir()
    monkeypatch.setattr("ortus.core.codegraph.shutil.which", lambda name: f"/bin/{name}")

    def _boom(self: CodeGraphAdapter, *args: object, **kwargs: object) -> dict:
        raise CodeGraphRpcError("server unavailable")

    monkeypatch.setattr(CodeGraphAdapter, "mcp_tools_call", _boom)
    probe = CodeGraphAdapter().probe(tmp_path, CodeGraphMode.AUTO)
    assert not probe.available
    assert "MCP tools/call failed" in (probe.reason or "")
