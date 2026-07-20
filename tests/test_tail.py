"""Tests for ortus tail (idzn.4 acceptance criteria)."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.tail import PREFIXES, _follow, _format_line

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


def test_prefixes_include_plan(tmp_path: Path) -> None:
    """ortus-emxo: plan-*.log files must be followed alongside grind/goal/ralph."""
    assert "plan-" in PREFIXES


def test_follow_picks_up_plan_log(tmp_path: Path) -> None:
    """ortus-emxo: plan-<ts>.log written by `ortus plan` is surfaced by tail."""
    logs = tmp_path / "logs"
    logs.mkdir()
    plan_log = logs / "plan-20260517-120000.log"
    plan_log.write_text('{"type":"assistant","message":{"content":"from plan"}}\n')

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    assert "from plan" in buf.getvalue()


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


def test_tail_renders_normalized_codegraph_events_without_verbose() -> None:
    line = json.dumps(
        {
            "type": "ortus.codegraph",
            "schema": 1,
            "kind": "query",
            "phase": "verification",
            "tool": "codegraph_search",
            "query": "Widget.render",
            "success": True,
            "hit": False,
        }
    )
    rendered = _format_line(line, show_tools=False, show_system=False)
    assert rendered is not None
    assert "[CODEGRAPH]" in rendered and "verification" in rendered and "miss" in rendered


def test_tail_distinguishes_codegraph_child_handshake_failure() -> None:
    rendered = _format_line(
        json.dumps(
            {
                "type": "ortus.codegraph",
                "kind": "handshake",
                "phase": "implementation",
                "success": False,
                "reason": "server unavailable",
            }
        ),
        show_tools=False,
        show_system=False,
    )
    assert rendered is not None
    assert "child handshake failed" in rendered
    assert "server unavailable" in rendered


def test_tail_fr003_no_beads(tmp_path: Path) -> None:
    bogus = tmp_path / "no-beads"
    bogus.mkdir()
    result = runner.invoke(app, ["tail", str(bogus)])
    assert result.exit_code == 1


def test_verbose_renders_tool_use_inside_assistant_content(tmp_path: Path) -> None:
    """ortus-tshw: assistant.content[].type=tool_use must surface under --verbose.

    Before the parity fix, Python tail only extracted ``.text`` from each part
    of an assistant message's content list, silently discarding tool_use
    entries — so operators watching --verbose missed every tool call.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    log.write_text(
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"calling Bash now"},'
        '{"type":"tool_use","name":"Bash","input":{"command":"ls"}}'
        "]}}\n"
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=True, show_system=True, iterations=1, out=buf)
    out = buf.getvalue()
    assert "calling Bash now" in out
    assert "Bash" in out and "ls" in out, f"expected tool_use rendered; saw:\n{out}"


def test_verbose_renders_user_tool_result(tmp_path: Path) -> None:
    """ortus-tshw: user.content[].type=tool_result must surface under --verbose."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    log.write_text(
        '{"type":"user","message":{"content":['
        '{"type":"tool_result","tool_use_id":"abc","content":"file contents here"}'
        "]}}\n"
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=True, show_system=False, iterations=1, out=buf)
    assert "file contents here" in buf.getvalue()


def test_user_text_messages_are_always_shown(tmp_path: Path) -> None:
    """ortus-tshw: user text content was silently dropped by the original port."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    log.write_text(
        '{"type":"user","message":{"content":"hi from operator"}}\n'
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    assert "hi from operator" in buf.getvalue()


def test_system_init_renders_as_banner_at_any_verbosity(tmp_path: Path) -> None:
    """ortus-tshw: system:init was bash's NEW SESSION banner; must be shown always."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    log.write_text(
        '{"type":"system","subtype":"init","session_id":"sess-xyz"}\n'
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "NEW SESSION" in out
    assert "sess-xyz" in out


def test_verbose_renders_every_real_stream_json_category(tmp_path: Path) -> None:
    """ortus-tshw parity acceptance: --verbose must include every category bash showed.

    Fixture mirrors real claude stream-json shapes captured from logs/goal-*.log:
    system:hook_started/hook_response/init plus assistant text/thinking/tool_use
    plus user tool_result. None of these should be silently dropped at --verbose.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-fixture.log"
    log.write_text(
        '{"type":"system","subtype":"hook_started","hook_name":"SessionStart"}\n'
        '{"type":"system","subtype":"init","session_id":"S1"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"plan-text"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"plan-think"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"true"}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"x","content":"plan-result"}]}}\n'
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=True, show_system=True, iterations=1, out=buf)
    out = buf.getvalue()
    for needle in ("hook_started", "NEW SESSION", "S1", "plan-text", "plan-think", "Bash", "plan-result"):
        assert needle in out, f"--verbose dropped {needle!r}; saw:\n{out}"


@pytest.mark.smoke
def test_tail_smoke_picks_up_new_grind_log(tmp_path: Path) -> None:
    """Smoke: realistic flow — start tailing, then a grind log appears."""
    logs = tmp_path / "logs"
    logs.mkdir()
    buf = io.StringIO()

    def _run() -> None:
        _follow(logs, raw=False, show_tools=False, show_system=False, iterations=2, out=buf)

    t = threading.Thread(target=_run)
    t.start()
    time.sleep(0.2)
    (logs / "grind-smoke.log").write_text(
        '{"type":"assistant","message":{"content":"working on bd-1"}}\n'
        '{"type":"assistant","message":{"content":"closed bd-1"}}\n'
    )
    t.join(timeout=5)
    out = buf.getvalue()
    assert "working on bd-1" in out
    assert "closed bd-1" in out
