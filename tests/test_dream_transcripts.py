"""Grind logs split into bead windows and prepared for a Dream reader."""

from __future__ import annotations

import json
from pathlib import Path

from ortus.core.dream_transcripts import (
    CUT_KEEP_BYTES,
    SKIPPED_UNKNOWN_SHAPE,
    BeadWindow,
    prepare_window,
    split_windows,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dream" / "grind-20261011-000000.log"


def _events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _window(lines: list[str], backend: str | None = "claude") -> BeadWindow:
    return BeadWindow(
        log_path=Path("grind-x.log"),
        run="grind-x",
        bead_id="ortus-x",
        start_line=1,
        end_line=len(lines),
        backend=backend,
        lines=tuple(lines),
    )


def test_split_opens_one_window_per_prep_marker() -> None:
    windows = split_windows(FIXTURE)

    assert [w.bead_id for w in windows] == ["ortus-aaa1", "ortus-bbb2"]
    assert [(w.start_line, w.end_line) for w in windows] == [(3, 11), (12, 15)]
    assert all(w.run == "grind-20261011-000000" for w in windows)
    assert all(w.backend == "claude" for w in windows)
    first = windows[0]
    assert len(first.lines) == first.end_line - first.start_line + 1
    assert first.lines[0].endswith("iter prep: worker will claim ortus-aaa1 via goal-prompt")
    assert first.lines[-1].endswith("worker closed ortus-aaa1 (tasks_completed=1)")


def test_split_keeps_the_run_header_out_of_every_window(tmp_path: Path) -> None:
    for window in split_windows(FIXTURE):
        assert not any("=== ortus grind started" in line for line in window.lines)

    log = tmp_path / "grind-1.log"
    log.write_text("[2026-10-11 00:00:00] === ortus grind started (shape) ===\nplain\n")
    assert split_windows(log) == ()


def test_split_marker_on_last_line_is_a_one_line_window(tmp_path: Path) -> None:
    log = tmp_path / "grind-2.log"
    log.write_bytes(
        b"header \xff\n[2026-10-11 00:00:00] iter prep: worker will claim ortus-z via goal-prompt"
    )

    (window,) = split_windows(log)

    assert (window.start_line, window.end_line) == (2, 2)
    assert window.backend is None
    assert len(window.lines) == 1


def test_cleanup_drops_envelope_noise_and_duplicate_tool_use_result() -> None:
    first, _ = split_windows(FIXTURE)

    prepared = prepare_window(first)
    events = _events(prepared.body)

    assert prepared.skipped is None
    types = [event["type"] for event in events]
    assert "rate_limit_event" not in types
    assert types == ["system", "ortus.codegraph", "assistant", "user", "result"]
    for event in events:
        if event["type"] == "ortus.codegraph":
            continue
        for key in ("session_id", "uuid", "parent_uuid", "parent_tool_use_id"):
            assert key not in event
    user = next(event for event in events if event["type"] == "user")
    assert "tool_use_result" not in user
    assert user["message"]["content"][0]["is_error"] is True
    assistant = next(event for event in events if event["type"] == "assistant")
    kinds = [block["type"] for block in assistant["message"]["content"]]
    assert kinds == ["thinking", "tool_use"]
    result = next(event for event in events if event["type"] == "result")
    assert result["num_turns"] == 3


def test_cleanup_keeps_text_and_non_stream_json_verbatim() -> None:
    first, _ = split_windows(FIXTURE)

    body_lines = prepare_window(first).body.splitlines()

    codegraph = next(line for line in first.lines if '"ortus.codegraph"' in line)
    assert codegraph in body_lines
    assert '"session_id":"kept"' in codegraph
    assert first.lines[0] in body_lines
    assert first.lines[-1] in body_lines


def test_cleanup_drops_tool_progress_and_keeps_truncated_json_as_text() -> None:
    truncated = '{"type":"assistant","message":{"content":[{"ty'
    window = _window(
        ['{"type":"tool_progress","tool_use_id":"t"}', "[1, 2]", truncated]
    )

    prepared = prepare_window(window)

    assert prepared.body.splitlines() == ["[1, 2]", truncated]


def test_cut_shortens_oversized_strings_and_counts_them() -> None:
    _, second = split_windows(FIXTURE)

    prepared = prepare_window(second)

    assert prepared.cuts == 1
    (user,) = [e for e in _events(prepared.body) if e["type"] == "user"]
    text = user["message"]["content"][0]["content"][0]["text"]
    assert text == "x" * CUT_KEEP_BYTES + " [cut 20 KB]"


def test_cut_leaves_a_string_exactly_at_the_limit() -> None:
    at_limit = "y" * 64
    over = "é" * 33  # 66 UTF-8 bytes
    line = json.dumps({"type": "assistant", "a": at_limit, "b": [[over]]})

    prepared = prepare_window(_window([line]), cut_bytes=64)

    (event,) = _events(prepared.body)
    assert prepared.cuts == 1
    assert event["a"] == at_limit
    assert event["b"][0][0] == over + " [cut 1 KB]"


def test_unknown_shape_backend_is_skipped(tmp_path: Path) -> None:
    log = tmp_path / "grind-20261011-000001.log"
    log.write_text(
        FIXTURE.read_text(encoding="utf-8").replace("backend=claude", "backend=codex", 1),
        encoding="utf-8",
    )

    windows = split_windows(log)
    prepared = [prepare_window(window) for window in windows]

    assert [w.backend for w in windows] == ["codex", "codex"]
    assert all(p.skipped == SKIPPED_UNKNOWN_SHAPE for p in prepared)
    assert all(p.body == "" and p.cuts == 0 for p in prepared)
