"""Tests for ortus tail (idzn.4 acceptance criteria)."""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.tail import _follow, _format_line

runner = CliRunner()


def test_follow_picks_up_existing_grind_and_ralph_logs(tmp_path: Path) -> None:
    """Acceptance #1: follows both grind-* and ralph-* files."""
    logs = tmp_path / "logs"
    logs.mkdir()
    grind = logs / "grind-20260516-001.log"
    ralph = logs / "ralph-20260515-009.log"
    grind.write_text('{"type":"assistant","message":{"content":"from grind"}}\n')
    ralph.write_text('{"type":"assistant","message":{"content":"from ralph"}}\n')

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "from grind" in out
    assert "from ralph" in out


def test_default_filter_drops_system_and_tools(tmp_path: Path) -> None:
    """Acceptance #2: default output filters stream-json into human-readable."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    log.write_text(
        '{"type":"system","subtype":"start"}\n'
        '{"type":"tool_use","name":"Bash","input":"ls"}\n'
        '{"type":"assistant","message":{"content":"working"}}\n'
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "working" in out
    assert "system:start" not in out
    assert "Bash" not in out


def test_raw_emits_verbatim(tmp_path: Path) -> None:
    """Acceptance #3: --raw emits raw lines verbatim."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    raw_lines = (
        '{"type":"system","subtype":"start"}\n'
        '{"type":"assistant","message":{"content":"verbatim"}}\n'
    )
    log.write_text(raw_lines)
    buf = io.StringIO()
    _follow(logs, raw=True, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert '{"type":"system","subtype":"start"}' in out
    assert '"content":"verbatim"' in out


def test_new_log_file_picked_up_within_two_seconds(tmp_path: Path) -> None:
    """Acceptance #4: new file appearing mid-tail is picked up within 2s."""
    logs = tmp_path / "logs"
    logs.mkdir()
    buf = io.StringIO()

    # Run _follow for 3 iterations (3s with 1s poll).
    def _run() -> None:
        _follow(logs, raw=False, show_tools=False, show_system=False, iterations=3, out=buf)

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.3)
    # Create the file mid-follow.
    new_log = logs / "grind-mid.log"
    new_log.write_text('{"type":"assistant","message":{"content":"newer"}}\n')
    thread.join(timeout=5)
    assert "newer" in buf.getvalue()


def test_tail_is_strictly_read_only(tmp_path: Path) -> None:
    """Acceptance #5: NFR-006 — no writes."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    log.write_text("hello\n")
    mtime_before = log.stat().st_mtime
    size_before = log.stat().st_size

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)

    assert log.stat().st_mtime == mtime_before
    assert log.stat().st_size == size_before


def test_format_line_passes_non_json_through() -> None:
    """Plain-text lines (timestamp banners etc.) pass through unfiltered."""
    assert _format_line(
        "[2026-05-16 10:00:00] grind.sh Started", show_tools=False, show_system=False
    ) == "[2026-05-16 10:00:00] grind.sh Started"


def test_format_line_returns_none_for_skipped_kinds() -> None:
    assert (
        _format_line('{"type":"system","subtype":"x"}', show_tools=False, show_system=False)
        is None
    )
    assert (
        _format_line('{"type":"tool_use","name":"x"}', show_tools=False, show_system=False)
        is None
    )


def test_format_line_emits_result_kind() -> None:
    assert (
        _format_line('{"type":"result","result":"ok"}', show_tools=False, show_system=False)
        == "[result] ok"
    )


def test_tail_fr003_no_beads(tmp_path: Path) -> None:
    bogus = tmp_path / "no-beads"
    bogus.mkdir()
    result = runner.invoke(app, ["tail", str(bogus)])
    assert result.exit_code == 1
