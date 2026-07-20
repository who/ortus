from __future__ import annotations

import json
from pathlib import Path

import pytest

from ortus.core.codegraph import (
    MAX_LABEL,
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
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
    probe = CodeGraphAdapter().probe(tmp_path, CodeGraphMode.AUTO, backend="codex")
    assert probe.available
    assert probe.capability is not None
    assert probe.capability.command == "/bin/codegraph"
    assert probe.capability.args == ("serve", "--mcp")


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
