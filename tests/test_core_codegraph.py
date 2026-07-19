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
