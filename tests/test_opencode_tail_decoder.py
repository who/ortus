"""opencode `opencode run --format json` decoder tests (ortus-t2kn.5).

Sibling of ``test_codex_tail_decoder.py`` with the same three conditions:

1. The decoder renders every element type in the Q1 capture — assistant
   text, built-in and MCP tool calls with their results, step usage, and
   step bookkeeping — asserted against a checked-in golden render.
2. It reads typed event fields, never free text. Renaming a typed field in
   an event must change the render.
3. A malformed/unparseable event fails loudly with a diagnostic instead of
   being silently skipped, and a part kind the decoder was not written for
   is labelled rather than guessed at.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from ortus.commands.tail import (
    OPENCODE_DECODE_ERROR_PREFIX,
    _follow,
    _format_line,
    _format_opencode_line,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
FIXTURE = FIXTURES / "opencode-run-events.jsonl"
GOLDEN = FIXTURES / "opencode-tail-golden.txt"


def _render(path: Path, *, show_tools: bool = True, show_system: bool = True) -> str:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rendered = _format_opencode_line(
            line, show_tools=show_tools, show_system=show_system
        )
        if rendered is not None:
            out.append(rendered)
    return "\n".join(out) + "\n"


def _lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def _first(kind: str, **match: str) -> str:
    for line in _lines():
        obj = json.loads(line)
        if obj["type"] != kind:
            continue
        if all(obj["part"].get(key) == value for key, value in match.items()):
            return line
    raise AssertionError(f"no {kind} event matching {match} in the fixture")


def _tool(name: str, status: str = "completed", **state: object) -> str:
    return json.dumps(
        {
            "type": "tool_use",
            "timestamp": 1788474287304,
            "sessionID": "ses_x",
            "part": {
                "type": "tool",
                "tool": name,
                "callID": "call-1",
                "state": {"status": status, **state},
            },
        }
    )


# ---------------------------------------------------------------------------
# 1. Renders every element type from the Q1 capture
# ---------------------------------------------------------------------------


def test_python_decoder_matches_golden_render() -> None:
    assert _render(FIXTURE) == GOLDEN.read_text(encoding="utf-8")


def test_golden_contains_every_element_type() -> None:
    golden = GOLDEN.read_text(encoding="utf-8")
    assert "<<< ASSISTANT\nOK" in golden
    assert "  [TOOL] bash\n" in golden
    assert '  {"command": "ls -la /work/probe"}' in golden
    assert "  [RESULT] bash: completed" in golden
    assert "  [MCP] codegraph_codegraph_explore\n" in golden
    assert '  {"query": "hello"}' in golden
    assert "  [RESULT] codegraph_codegraph_explore: completed" in golden
    assert "  [USAGE] input=8876 cached=0 output=23 reasoning=0 reason=stop" in golden
    assert "reason=tool-calls" in golden
    assert "[SYS] step_start" in golden
    assert "sessionID" not in golden


def test_default_verbosity_shows_text_and_turn_usage_only() -> None:
    out = _render(FIXTURE, show_tools=False, show_system=False)
    assert out.count("<<< ASSISTANT") == 3
    assert out.count("reason=stop") == 3
    assert "reason=tool-calls" not in out
    assert "[TOOL]" not in out
    assert "[MCP]" not in out
    assert "[RESULT]" not in out
    assert "[SYS]" not in out


def test_tool_and_step_events_respect_verbosity_gates() -> None:
    tool = _first("tool_use", tool="bash")
    start = _first("step_start")
    between = _first("step_finish", reason="tool-calls")
    stop = _first("step_finish", reason="stop")
    text = _first("text")
    assert _format_opencode_line(tool, show_tools=False, show_system=True) is None
    assert _format_opencode_line(tool, show_tools=True, show_system=False) is not None
    assert _format_opencode_line(start, show_tools=True, show_system=False) is None
    assert _format_opencode_line(start, show_tools=False, show_system=True) == "[SYS] step_start"
    assert _format_opencode_line(between, show_tools=True, show_system=False) is None
    assert "reason=tool-calls" in (
        _format_opencode_line(between, show_tools=False, show_system=True) or ""
    )
    assert "reason=stop" in (_format_opencode_line(stop, show_tools=False, show_system=False) or "")
    assert "<<< ASSISTANT" in (_format_opencode_line(text, show_tools=False, show_system=False) or "")


# ---------------------------------------------------------------------------
# 2. Reads typed fields, never free text
# ---------------------------------------------------------------------------


def test_tool_name_is_read_from_the_typed_field() -> None:
    line = _first("tool_use", tool="bash")
    renamed = line.replace('"tool":"bash"', '"name":"bash"')
    assert '"name":"bash"' in renamed
    rendered = _format_opencode_line(renamed, show_tools=True, show_system=False) or ""
    assert rendered.startswith("  [TOOL] tool\n")
    assert "[TOOL] bash" not in rendered


def test_mcp_label_needs_a_registered_server_prefix() -> None:
    def label(name: str) -> str:
        rendered = _format_opencode_line(
            _tool(name, input={"query": "x"}), show_tools=True, show_system=False
        )
        return (rendered or "").splitlines()[0]

    assert label("codegraph_codegraph_explore") == "  [MCP] codegraph_codegraph_explore"
    assert label("codegraph_") == "  [TOOL] codegraph_"
    assert label("codegraph") == "  [TOOL] codegraph"
    assert label("bash") == "  [TOOL] bash"


def test_failed_tool_renders_as_error_with_its_message() -> None:
    rendered = _format_opencode_line(
        _tool("bash", "error", input={"command": "false"}, error="exit status 1"),
        show_tools=True,
        show_system=False,
    )
    assert rendered == (
        "  [TOOL] bash\n"
        '  {"command": "false"}\n'
        "  [RESULT] bash: ERROR\n"
        "  exit status 1"
    )


def test_empty_and_dotted_tool_names_render_without_crashing() -> None:
    empty = _format_opencode_line(_tool("", input={}), show_tools=True, show_system=False)
    assert empty == "  [TOOL] tool\n  [RESULT] tool: completed"
    dotted = _format_opencode_line(
        _tool("codegraph.codegraph_explore", input={"query": "x"}, output=""),
        show_tools=True,
        show_system=False,
    )
    assert dotted == (
        "  [TOOL] codegraph.codegraph_explore\n"
        '  {"query": "x"}\n'
        "  [RESULT] codegraph.codegraph_explore: completed"
    )


def test_usage_counts_come_from_typed_paths_not_the_raw_line() -> None:
    line = _first("step_finish", reason="stop")
    renamed = line.replace('"input":8876', '"in":8876')
    assert '"in":8876' in renamed
    rendered = _format_opencode_line(renamed, show_tools=False, show_system=False) or ""
    assert "input=0" in rendered
    assert "8876" not in rendered


# ---------------------------------------------------------------------------
# 3. Malformed events fail loudly; unknown parts are labelled
# ---------------------------------------------------------------------------


def test_truncated_event_fails_loudly() -> None:
    truncated = _first("tool_use", tool="bash")[:80]
    rendered = _format_opencode_line(truncated, show_tools=True, show_system=True)
    assert rendered is not None
    assert rendered.startswith(OPENCODE_DECODE_ERROR_PREFIX)
    assert truncated[:40] in rendered


def test_event_without_a_type_field_fails_loudly() -> None:
    rendered = _format_opencode_line(
        '{"part":{"type":"text","text":"OK"}}', show_tools=True, show_system=True
    )
    assert rendered is not None and rendered.startswith(OPENCODE_DECODE_ERROR_PREFIX)
    assert "no `type` field" in rendered


def test_non_object_event_fails_loudly() -> None:
    rendered = _format_opencode_line("{}", show_tools=True, show_system=True)
    assert rendered is not None and rendered.startswith(OPENCODE_DECODE_ERROR_PREFIX)


def test_unknown_part_kind_is_labelled_not_guessed() -> None:
    line = json.dumps(
        {"type": "reasoning", "part": {"type": "reasoning", "text": "thinking aloud"}}
    )
    assert _format_opencode_line(line, show_tools=True, show_system=True) == "[SYS] reasoning"
    assert _format_opencode_line(line, show_tools=True, show_system=False) is None
    bare = json.dumps({"type": "tool_use"})
    assert _format_opencode_line(bare, show_tools=True, show_system=True) == "[SYS] tool_use"
    assert _format_opencode_line(bare, show_tools=True, show_system=False) is None


def test_opencode_events_are_not_grok_crumbs_to_the_claude_decoder() -> None:
    """The decoders are distinct: a misrouted opencode log is never read as Grok.

    The stream-json decoder shows nothing for opencode text and step events
    and, for a tool event, only its legacy top-level `tool_use` branch with
    no name — never a Grok think/text/tool crumb, which shares the `text`
    type name and would otherwise coalesce the worker's answers into a
    paragraph of the wrong backend.
    """
    grok_prefixes = ("  think  ", "  text   ", "  tool   ", "  done   ", "  fail   ")
    for line in _lines():
        rendered = _format_line(line, show_tools=True, show_system=True)
        kind = json.loads(line)["type"]
        if kind == "tool_use":
            assert rendered is not None and rendered.startswith("  [TOOL] ?")
        else:
            assert rendered is None, (kind, rendered)
        assert not (rendered or "").startswith(grok_prefixes)
    text = _first("text")
    assert "OK" in (_format_opencode_line(text, show_tools=False, show_system=False) or "")


def test_follow_uses_the_opencode_decoder_when_requested(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "grind-opencode.log").write_text(
        _first("text") + "\n" + _first("tool_use", tool="bash")[:80] + "\n",
        encoding="utf-8",
    )
    out = io.StringIO()
    err = io.StringIO()
    _follow(
        logs,
        raw=False,
        show_tools=True,
        show_system=False,
        iterations=1,
        out=out,
        err=err,
        opencode=True,
    )
    assert "<<< ASSISTANT\nOK" in out.getvalue()
    assert OPENCODE_DECODE_ERROR_PREFIX in out.getvalue()
    assert OPENCODE_DECODE_ERROR_PREFIX in err.getvalue()
