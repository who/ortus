"""Codex `codex exec --json` decoder tests (ortus-5hae / FR-007).

Three acceptance conditions, one per section below:

1. The decoder renders every element type in the Q2 fixture (ortus-l75g):
   assistant text, command calls, and token usage — asserted against a
   checked-in golden render.
2. It reads typed event fields, never free text. Renaming a typed field in
   an event must change the render; the decoder must not recover the value
   by pattern-matching the raw line.
3. A malformed/unparseable event fails loudly with a diagnostic instead of
   being silently skipped.

The bash decoder in ortus/tail.sh is driven through its ``--decode`` mode
against the same golden, so the two implementations cannot drift apart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ortus.commands.tail import (
    CODEX_DECODE_ERROR_PREFIX,
    _format_codex_line,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
HAPPY_FIXTURE = FIXTURES / "codex-exec-events.jsonl"
FAILED_FIXTURE = FIXTURES / "codex-exec-events-failed.jsonl"
HAPPY_GOLDEN = FIXTURES / "codex-tail-golden.txt"
FAILED_GOLDEN = FIXTURES / "codex-tail-golden-failed.txt"
TAIL_SH = REPO_ROOT / "ortus" / "tail.sh"


def _render(path: Path, *, show_tools: bool = True, show_system: bool = True) -> str:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rendered = _format_codex_line(line, show_tools=show_tools, show_system=show_system)
        if rendered is not None:
            out.append(rendered)
    return "\n".join(out) + "\n"


def _render_sh(path: Path, *, tail_sh: Path = TAIL_SH) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(tail_sh), "--codex", "--decode", str(path)],
        capture_output=True,
        text=True,
        env={
            "NO_COLOR": "1",
            "SHOW_TOOLS": "true",
            "SHOW_SYSTEM": "true",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # tail.sh mktemps a scratch file at startup; honour the sandbox's
            # writable temp dir rather than assuming /tmp.
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        },
    )


requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
requires_bash_tail = pytest.mark.skipif(
    not (REPO_ROOT / "ortus" / "tail.sh").is_file(),
    reason="bash-era tail.sh has been archived; Python decoder is canonical",
)


# ---------------------------------------------------------------------------
# 1. Renders every element type from the Q2 fixture
# ---------------------------------------------------------------------------


def test_python_decoder_matches_golden_render() -> None:
    assert _render(HAPPY_FIXTURE) == HAPPY_GOLDEN.read_text(encoding="utf-8")


def test_python_decoder_matches_golden_render_for_failed_turn() -> None:
    assert _render(FAILED_FIXTURE) == FAILED_GOLDEN.read_text(encoding="utf-8")


def test_golden_contains_all_three_element_types() -> None:
    """Assistant text, command calls, and token usage all reach the render."""
    golden = HAPPY_GOLDEN.read_text(encoding="utf-8")
    assert "<<< ASSISTANT" in golden
    assert "Wrote `spike-note.txt`" in golden
    assert "[TOOL] command_execution" in golden
    assert "cat spike-note.txt" in golden
    assert "[USAGE] input=5800 cached=1280 output=230 reasoning=80" in golden


def test_failed_command_renders_as_error_with_exit_code() -> None:
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "i",
                "type": "command_execution",
                "command": "false",
                "aggregated_output": "",
                "exit_code": 3,
                "status": "failed",
            },
        }
    )
    assert _format_codex_line(line, show_tools=True, show_system=False) == (
        "  [RESULT] command_execution: ERROR (exit 3)"
    )


def test_turn_failed_is_shown_at_default_verbosity() -> None:
    line = '{"type":"turn.failed","error":{"message":"high demand"}}'
    assert (
        _format_codex_line(line, show_tools=False, show_system=False)
        == "  [TURN FAILED] high demand"
    )


def test_tool_and_thinking_events_respect_verbosity_gates() -> None:
    cmd = (
        '{"type":"item.started","item":{"id":"i","type":"command_execution",'
        '"command":"ls","aggregated_output":"","exit_code":null,'
        '"status":"in_progress"}}'
    )
    reasoning = '{"type":"item.completed","item":{"id":"i","type":"reasoning","text":"hmm"}}'
    assert _format_codex_line(cmd, show_tools=False, show_system=True) is None
    assert _format_codex_line(reasoning, show_tools=True, show_system=False) is None
    assert _format_codex_line(cmd, show_tools=True, show_system=False) is not None
    assert _format_codex_line(reasoning, show_tools=False, show_system=True) is not None


@requires_jq
@requires_bash_tail
def test_bash_decoder_matches_the_same_golden() -> None:
    """ortus/tail.sh --decode must render byte-identically to the Python decoder."""
    proc = _render_sh(HAPPY_FIXTURE)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == HAPPY_GOLDEN.read_text(encoding="utf-8"), proc.stdout


@requires_jq
def test_template_tail_sh_decoder_matches_the_same_golden() -> None:
    """NFR-003: the template mirror decodes identically, not just ortus/."""
    proc = _render_sh(HAPPY_FIXTURE, tail_sh=REPO_ROOT / "template" / "ortus" / "tail.sh")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == HAPPY_GOLDEN.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. Typed fields only — never free-text greps
# ---------------------------------------------------------------------------


def test_assistant_text_is_read_from_the_typed_item_type() -> None:
    """An event whose item.type is not agent_message must not render as one.

    A free-text decoder would still surface the `text` value here; a typed
    one reports it as an unknown item kind.
    """
    line = '{"type":"item.completed","item":{"id":"i","type":"mystery","text":"leak"}}'
    rendered = _format_codex_line(line, show_tools=True, show_system=True)
    assert rendered == "[SYS] item.mystery"
    assert "leak" not in rendered


def test_usage_counts_come_from_typed_paths_not_the_raw_line() -> None:
    line = '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":9}}'
    assert (
        _format_codex_line(line, show_tools=False, show_system=False)
        == "  [USAGE] input=7 cached=0 output=9 reasoning=0"
    )


def test_unknown_top_level_event_is_labelled_not_guessed() -> None:
    line = '{"type":"turn.paused","note":"whatever"}'
    assert _format_codex_line(line, show_tools=True, show_system=True) == "[SYS] turn.paused"
    assert _format_codex_line(line, show_tools=True, show_system=False) is None


# ---------------------------------------------------------------------------
# 3. Malformed events fail loudly
# ---------------------------------------------------------------------------


def test_truncated_event_fails_loudly() -> None:
    truncated = '{"type":"item.completed","item":{"id":"i","type":"agent_'
    rendered = _format_codex_line(truncated, show_tools=True, show_system=True)
    assert rendered is not None
    assert rendered.startswith(CODEX_DECODE_ERROR_PREFIX)
    assert truncated in rendered


def test_event_without_a_type_field_fails_loudly() -> None:
    rendered = _format_codex_line('{"item":{"type":"agent_message"}}', show_tools=True, show_system=True)
    assert rendered is not None and rendered.startswith(CODEX_DECODE_ERROR_PREFIX)


def test_non_object_event_fails_loudly() -> None:
    rendered = _format_codex_line("{}", show_tools=True, show_system=True)
    assert rendered is not None and rendered.startswith(CODEX_DECODE_ERROR_PREFIX)


@requires_jq
@requires_bash_tail
def test_bash_decoder_fails_loudly_and_nonzero_on_truncated_event(tmp_path: Path) -> None:
    bad = tmp_path / "codex-truncated.jsonl"
    bad.write_text('{"type":"item.completed","item":{"id":"i","type":"agent_\n', encoding="utf-8")
    proc = _render_sh(bad)
    assert proc.returncode != 0
    assert CODEX_DECODE_ERROR_PREFIX in proc.stderr
    assert CODEX_DECODE_ERROR_PREFIX in proc.stdout


# ---------------------------------------------------------------------------
# Decoder selection: the Claude branch is untouched (NFR-001)
# ---------------------------------------------------------------------------


def test_codex_events_are_invisible_to_the_claude_decoder() -> None:
    """Sanity: the two decoders are distinct; codex events are not stream-json."""
    from ortus.commands.tail import _format_line

    line = '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"hi"}}'
    assert _format_line(line, show_tools=True, show_system=True) is None
    assert "hi" in (_format_codex_line(line, show_tools=True, show_system=True) or "")


def test_follow_uses_the_codex_decoder_when_requested(tmp_path: Path) -> None:
    import io

    from ortus.commands.tail import _follow

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "goal-codex.log").write_text(
        '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"from codex"}}\n',
        encoding="utf-8",
    )
    buf = io.StringIO()
    _follow(logs, raw=False, show_tools=False, show_system=False, iterations=1, out=buf, codex=True)
    assert "from codex" in buf.getvalue()
