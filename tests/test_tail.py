"""Tests for ortus tail (idzn.4 acceptance criteria)."""

from __future__ import annotations

import io
import json
import os
import re
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
    _follow(
        logs,
        raw=False,
        show_tools=False,
        show_system=False,
        iterations=1,
        out=buf,
        follow_all=True,
    )
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
        _follow(
            logs, raw=False, show_tools=False, show_system=False, iterations=3, out=buf
        )

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
    assert (
        _format_line(
            "[2026-05-16 10:00:00] grind.sh Started",
            show_tools=False,
            show_system=False,
        )
        == "[2026-05-16 10:00:00] grind.sh Started"
    )


def test_format_line_returns_none_for_skipped_kinds() -> None:
    assert (
        _format_line(
            '{"type":"system","subtype":"x"}', show_tools=False, show_system=False
        )
        is None
    )
    assert (
        _format_line(
            '{"type":"tool_use","name":"x"}', show_tools=False, show_system=False
        )
        is None
    )


def test_format_line_emits_result_kind() -> None:
    assert (
        _format_line(
            '{"type":"result","result":"ok"}', show_tools=False, show_system=False
        )
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
    assert (
        "[CODEGRAPH]" in rendered and "verification" in rendered and "miss" in rendered
    )


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


def test_tail_renders_candidate_verdict_without_verbose() -> None:
    rendered = _format_line(
        json.dumps(
            {
                "type": "ortus.verdict",
                "schema": 1,
                "decision": "pass",
                "candidate_hash": "abcdef0123456789",
                "reason": "",
            }
        ),
        show_tools=False,
        show_system=False,
    )
    assert rendered == "[VERDICT] PASS candidate=abcdef012345"


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
    log.write_text('{"type":"user","message":{"content":"hi from operator"}}\n')
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    assert "hi from operator" in buf.getvalue()


def test_system_init_renders_as_banner_at_any_verbosity(tmp_path: Path) -> None:
    """ortus-tshw: system:init was bash's NEW SESSION banner; must be shown always."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-1.log"
    log.write_text('{"type":"system","subtype":"init","session_id":"sess-xyz"}\n')
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
    for needle in (
        "hook_started",
        "NEW SESSION",
        "S1",
        "plan-text",
        "plan-think",
        "Bash",
        "plan-result",
    ):
        assert needle in out, f"--verbose dropped {needle!r}; saw:\n{out}"


@pytest.mark.smoke
def test_tail_smoke_picks_up_new_grind_log(tmp_path: Path) -> None:
    """Smoke: realistic flow — start tailing, then a grind log appears."""
    logs = tmp_path / "logs"
    logs.mkdir()
    buf = io.StringIO()

    def _run() -> None:
        _follow(
            logs, raw=False, show_tools=False, show_system=False, iterations=2, out=buf
        )

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


# --- ortus-fc2q: attach-time history cap (--lines/-n) ----------------------


def _assistant_line(i: int) -> str:
    return json.dumps({"type": "assistant", "message": {"content": f"L{i:05d}"}})


def _write_big_log(logs: Path, name: str, total: int) -> Path:
    log = logs / name
    log.write_text("".join(_assistant_line(i) + "\n" for i in range(total)))
    return log


def test_attach_caps_history_at_default(tmp_path: Path) -> None:
    """AC-1: attaching to a long log renders only its last 2,000 lines."""
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_big_log(logs, "grind-big.log", 2100)

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "L00100" in out  # first kept line (2100 - 2000)
    assert "L00099" not in out  # last trimmed line
    assert "L02099" in out  # newest line


def test_skip_notice_names_count_and_escape_hatch(tmp_path: Path) -> None:
    """AC-2: the notice names the trimmed count, the log, and --lines 0."""
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_big_log(logs, "grind-big.log", 2100)

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "SKIPPED 100 earlier lines: grind-big.log" in out
    assert "--lines 0" in out


def _expected_full_render(log: Path) -> str:
    """Today's uncapped output for `log`: banner, then every rendered line."""
    expected = [f"=== TAILING: {log.name} ==="]
    for line in log.read_text().splitlines():
        rendered = _format_line(line, show_tools=False, show_system=False)
        if rendered is not None:
            expected.append(rendered)
    return "\n".join(expected) + "\n"


def test_lines_zero_is_byte_identical_full_history(tmp_path: Path) -> None:
    """AC-3: --lines 0 reproduces the full-history output byte-for-byte."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = _write_big_log(logs, "grind-big.log", 2100)

    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=False,
        show_system=False,
        iterations=1,
        out=buf,
        lines=0,
    )
    assert buf.getvalue() == _expected_full_render(log)


def test_short_log_renders_without_notice(tmp_path: Path) -> None:
    """AC-4: a log at or under the cap renders identically, with no notice."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = _write_big_log(logs, "grind-small.log", 5)

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    assert buf.getvalue() == _expected_full_render(log)
    assert "SKIPPED" not in buf.getvalue()


def test_raw_mode_obeys_the_cap(tmp_path: Path) -> None:
    """AC-5: --raw trims the same attach backlog as the formatted view."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "grind-raw.log"
    log.write_text("".join(f"raw-{i:05d}\n" for i in range(2100)))

    buf = io.StringIO()
    _follow(logs, raw=True, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "raw-00100" in out
    assert "raw-00099" not in out
    assert "SKIPPED 100 earlier lines: grind-raw.log" in out


def test_follow_after_attach_is_unchanged(tmp_path: Path) -> None:
    """AC-6: lines appended after attach render in full, with no new notice."""
    logs = tmp_path / "logs"
    logs.mkdir()
    log = _write_big_log(logs, "grind-live.log", 2100)

    buf = io.StringIO()

    def _run() -> None:
        _follow(
            logs, raw=False, show_tools=False, show_system=False, iterations=2, out=buf
        )

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.3)
    with log.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(
                json.dumps(
                    {"type": "assistant", "message": {"content": f"appended-{i}"}}
                )
                + "\n"
            )
    thread.join(timeout=5)
    out = buf.getvalue()
    for i in range(3):
        assert f"appended-{i}" in out
    assert out.count("SKIPPED") == 1  # attach notice only; follow never trims


def test_multiple_logs_cap_independently(tmp_path: Path) -> None:
    """AC-7: each discovered log gets its own cap and its own notice."""
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_big_log(logs, "grind-a.log", 2010)
    _write_big_log(logs, "grind-b.log", 2020)
    small = logs / "grind-c.log"
    small.write_text(_assistant_line(0) + "\n")

    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=False,
        show_system=False,
        iterations=1,
        out=buf,
        follow_all=True,
        lines=2000,
    )
    out = buf.getvalue()
    assert "SKIPPED 10 earlier lines: grind-a.log" in out
    assert "SKIPPED 20 earlier lines: grind-b.log" in out
    assert out.count("SKIPPED") == 2
    assert "L00000" in out  # the short log is untouched
    assert "=== TAILING: grind-a.log ===" in out
    assert "=== TAILING: grind-b.log ===" in out
    assert "=== TAILING: grind-c.log ===" in out


# --- ortus-rjsp: default newest-only follow --------------------------------


def _assistant_payload(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": text}}) + "\n"


def test_default_follow_renders_only_newest_of_two_grind_logs(tmp_path: Path) -> None:
    """AC-1: two grind logs — only the newest banner and unique lines appear."""
    logs = tmp_path / "logs"
    logs.mkdir()
    older = logs / "grind-older.log"
    newer = logs / "grind-newer.log"
    older.write_text(_assistant_payload("older-unique-line"))
    newer.write_text(_assistant_payload("newer-unique-line"))
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))

    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "=== TAILING: grind-newer.log ===" in out
    assert "newer-unique-line" in out
    assert "=== TAILING: grind-older.log ===" not in out
    assert "older-unique-line" not in out


def test_follow_switches_to_newer_log_and_drops_old_appends(tmp_path: Path) -> None:
    """AC-2: a newer matching log mid-follow switches; old-file appends vanish."""
    logs = tmp_path / "logs"
    logs.mkdir()
    older = logs / "grind-20260814-000001.log"
    current = logs / "grind-20260814-000002.log"
    older.write_text(_assistant_payload("older-start"))
    current.write_text(_assistant_payload("current-unique"))
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(current, (1_700_000_100, 1_700_000_100))

    buf = io.StringIO()

    def _run() -> None:
        _follow(
            logs, raw=False, show_tools=False, show_system=False, iterations=3, out=buf
        )

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.3)
    incoming = logs / "grind-20260814-000003.log"
    incoming.write_text(_assistant_payload("incoming-unique"))
    os.utime(incoming, (1_700_000_200, 1_700_000_200))
    time.sleep(1.2)
    with older.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_payload("older-append-unique"))
    # Keep the old file older by mtime so the append is a follow-on write,
    # not a "this file just became newest" switch.
    os.utime(older, (1_700_000_000, 1_700_000_000))
    thread.join(timeout=6)
    out = buf.getvalue()
    assert "=== TAILING: grind-20260814-000003.log ===" in out
    assert "incoming-unique" in out
    assert "older-append-unique" not in out
    assert "=== TAILING: grind-20260814-000001.log ===" not in out


def test_empty_logs_dir_idles_without_banner(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    assert buf.getvalue() == ""


def test_initial_files_are_followed_exactly_not_remapped(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    older = logs / "grind-older.log"
    newer = logs / "grind-newer.log"
    older.write_text(_assistant_payload("injected-older"))
    newer.write_text(_assistant_payload("uninjected-newer"))
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))

    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=False,
        show_system=False,
        iterations=1,
        out=buf,
        initial_files=[older],
    )
    out = buf.getvalue()
    assert "injected-older" in out
    assert "uninjected-newer" not in out
    assert "=== TAILING: grind-newer.log ===" not in out


def test_tail_help_names_newest_default_and_all_flag() -> None:
    result = runner.invoke(app, ["tail", "--help"])
    assert result.exit_code == 0
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout or result.output)
    assert "--all" in text
    assert "newest" in text.lower()


# --- ortus-zt5n.7: Grok streaming-json decoder -----------------------------


_GROK_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "grok-stream-events.jsonl"


def _write_grok_log(logs: Path, name: str = "grind-grok.log") -> Path:
    log = logs / name
    log.write_text(_GROK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return log


def _follow_grok(
    tmp_path: Path, *, raw: bool = False, show_tools: bool = False, show_system: bool = False
) -> str:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    _write_grok_log(logs)
    buf = io.StringIO()
    _follow(
        logs,
        raw=raw,
        show_tools=show_tools,
        show_system=show_system,
        iterations=1,
        out=buf,
    )
    return buf.getvalue()


def test_grok_follow_coalesces_thought_text_and_summarizes_tools(tmp_path: Path) -> None:
    """Default tail on a Grok grind log: coalesced think/text, one tool line."""
    out = _follow_grok(tmp_path)
    assert "  think  The user wants a plan." in out
    assert "  think  I need to inspect the leftover state." in out
    assert "  text   I'll inspect the leftover state." in out
    assert out.count("  think  ") == 2
    assert out.count("  text   ") == 1
    assert out.count("  tool   ") == 3
    assert "  tool   search_tool  codegraph explore repository orientation" in out
    assert "  tool   read_file  src/ortus/commands/tail.py" in out
    assert "  tool   run_terminal_command  bd prime" in out
    assert "  done   tool" in out
    assert "  fail   tool" in out
    assert "[2026-08-14 14:08:40] iter 1: spawning grok (single-issue worker)" in out
    assert '{"type":"thought"' not in out
    assert '{"type":"tool_call"' not in out
    assert "available_commands" not in out
    assert "input_tokens" not in out
    assert "  plan   " not in out


def test_grok_raw_emits_original_json_lines(tmp_path: Path) -> None:
    """--raw / raw=True keeps every fixture line, including dropped kinds."""
    fixture = _GROK_FIXTURE.read_text(encoding="utf-8")
    out = _follow_grok(tmp_path, raw=True)
    for line in fixture.splitlines():
        assert line in out
    assert '{"type":"usage"' in out
    assert '{"type":"available_commands"' in out


def test_grok_default_omits_usage_and_available_commands(tmp_path: Path) -> None:
    out = _follow_grok(tmp_path)
    assert "available_commands" not in out
    assert '"type":"usage"' not in out
    assert "input_tokens" not in out
    assert "cache_read_input_tokens" not in out


def test_grok_tools_and_system_flags_do_not_hide_default_view(tmp_path: Path) -> None:
    """AC-1: default (no --tools/--system) still shows Grok think/text/tool."""
    out = _follow_grok(tmp_path, show_tools=False, show_system=False)
    assert "  think  The user wants a plan." in out
    assert "  text   I'll inspect the leftover state." in out
    assert "  tool   search_tool  codegraph explore repository orientation" in out
    assert "  done   tool" in out
    verbose = _follow_grok(tmp_path, show_tools=True, show_system=True)
    assert "  plan   [done] handshake" in verbose
    assert "  tool   read_file  src/ortus/commands/tail.py" in verbose


def test_grok_truncated_json_does_not_crash(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "grind-grok.log").write_text(
        '{"type":"thought","data":"partial\n'
        '{"type":"tool_call","toolName":"read_file","rawInput":{"target_file":"x.py"}\n',
        encoding="utf-8",
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf)
    out = buf.getvalue()
    assert "partial" in out
    assert "read_file" in out or "tool_call" in out


def test_grok_format_line_standalone_flushes_one_crumb() -> None:
    """_format_line without shared state still renders a Grok crumb."""
    rendered = _format_line(
        '{"type":"thought","data":"Hello from grok"}',
        show_tools=False,
        show_system=False,
    )
    assert rendered == "  think  Hello from grok"


# --- ortus-haxd: small --lines must keep the current think paragraph --------


def test_verbose_lines_one_keeps_current_grok_think_paragraph(tmp_path: Path) -> None:
    """AC-1: many type=thought crumbs ending in usage still coalesce under --lines 1."""
    logs = tmp_path / "logs"
    logs.mkdir()
    older = "".join(
        json.dumps({"type": "thought", "data": word}) + "\n"
        for word in ("Old", " paragraph.", "\n")
    )
    current = "".join(
        json.dumps({"type": "thought", "data": word}) + "\n"
        for word in ("Now", " I", " will", " inspect", " the", " leftover", " state.")
    )
    usage = json.dumps({"type": "usage", "input_tokens": 12}) + "\n"
    (logs / "grind-grok-crumbs.log").write_text(older + current + usage, encoding="utf-8")

    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=True,
        show_system=True,
        iterations=1,
        out=buf,
        lines=1,
    )
    out = buf.getvalue()
    assert "  think  Now I will inspect the leftover state." in out
    assert "  think  Old paragraph." not in out
    assert "input_tokens" not in out
    assert "SKIPPED" in out


def test_verbose_small_lines_keeps_prior_claude_thinking(tmp_path: Path) -> None:
    """AC-2: last raw line is not thinking; previous assistant thinking still prints."""
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"old-{i}"}]},
            }
        )
        for i in range(20)
    ]
    rows.append(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "thinking", "thinking": "weigh the attach cap"}]
                },
            }
        )
    )
    rows.append(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "ok"}
                    ]
                },
            }
        )
    )
    (logs / "grind-claude.log").write_text(
        "".join(row + "\n" for row in rows), encoding="utf-8"
    )

    buf = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=True,
        show_system=True,
        iterations=1,
        out=buf,
        lines=1,
    )
    out = buf.getvalue()
    assert "(thinking)" in out
    assert "weigh the attach cap" in out
    assert "old-0" not in out


def test_raw_lines_one_is_still_the_last_raw_line(tmp_path: Path) -> None:
    """--raw --lines 1 stays the last raw line, even when that line is usage."""
    logs = tmp_path / "logs"
    logs.mkdir()
    thought = json.dumps({"type": "thought", "data": "hidden by raw cap"}) + "\n"
    usage = json.dumps({"type": "usage", "input_tokens": 9}) + "\n"
    (logs / "grind-raw-cap.log").write_text(thought + usage, encoding="utf-8")

    buf = io.StringIO()
    _follow(
        logs,
        raw=True,
        show_tools=True,
        show_system=True,
        iterations=1,
        out=buf,
        lines=1,
    )
    out = buf.getvalue()
    assert '{"type": "usage", "input_tokens": 9}' in out
    assert "hidden by raw cap" not in out
