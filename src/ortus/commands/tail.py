"""ortus tail <repo> — follow logs/{grind,goal,ralph,plan}-*.log (idzn.4).

Strictly read-only (NFR-006). Default formatting filters claude stream-json
into human-readable turn boundaries; --raw emits lines verbatim. Default
follow attaches only the newest matching log and switches if a newer one
appears; --all restores multi-file follow. Polls the logs/ directory every
1s for new files matching the back-compat prefixes.

Verbosity contract (parity with legacy ortus/tail.sh; ortus-eomm):

    Always shown (any verbosity):
      - new-file banners            (bold magenta "=== TAILING: filename ===")
      - non-JSON lines              (pattern-coloured; see below)
      - assistant text content      (bold green "<<< ASSISTANT" banner + green body)
      - user text content           (bold blue ">>> USER" banner + blue body)
      - system:init events          (bold magenta "=== NEW SESSION ===" + magenta id)
      - top-level result events     (cyan "[RESULT] tool: subtype" + dim body;
                                     red on subtype=error)

    --tools / -t:
      - assistant tool_use          (yellow "  [TOOL] name" + dim input)
      - user tool_result            (cyan "  [result] body")

    --system / -s:
      - assistant thinking          (dim "  (thinking) body")
      - all other system subtypes   (dim "[SYS] subtype")

    --verbose / -v:  equivalent to --tools --system

    --assistant / -a:
      - suppress USER text (mirrors bash tail.sh ASSISTANT_ONLY)

    Grok streaming-json (type thought/text/tool_call/tool_call_update):
      Always shown (any verbosity, including default): coalesced think/text
      paragraphs, one tool line per tool_call, done/fail on tool_call_update.
      usage and available_commands are dropped. --tools does not hide Grok
      tools (they are the default view). --system also shows Grok plan entries.

    Non-JSON line colouring (mirrors bash format_line non-JSON branch):
      - "===..." lines                bold cyan (preceded by a blank line)
      - "Processing:" / "Found..."    cyan
      - lines matching error|Error|ERROR     red
      - lines matching success|Success|completed   green
      - everything else               dim

Colour palette mirrors ortus/tail.sh setup_colors() exactly so the two
implementations stay byte-comparable. Respects NO_COLOR
(https://no-color.org/) and emits plain text when stdout is not a TTY.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterable, Optional

import typer

from ortus.core import output
from ortus.core.agent import BackendError, resolve_backend
from ortus.core.local_backend import LOCAL_TABLE_BACKENDS
from ortus.core.repo import resolve_repo
from ortus.core.runstate import (
    OPENCODE_STEP_STOP,
    _mtime,
    is_grok_event as _is_grok_event,
    opencode_mcp_server as _opencode_mcp_server,
    summarize_grok_tool as _summarize_grok_tool,
)

PREFIXES = ("grind-", "goal-", "ralph-", "plan-")
POLL_SECONDS = 1.0

#: Input lines of existing history rendered when first attaching to a log.
#: 0 means unlimited. Overridable per run via --lines/-n.
DEFAULT_ATTACH_LINES = 2000


# ---------------------------------------------------------------------------
# Colour palette — literal ANSI escapes mirroring ortus/tail.sh setup_colors()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Palette:
    """ANSI codes used by the renderer.

    Field names mirror the shell variables in ortus/tail.sh setup_colors()
    so the two implementations stay byte-comparable. Each field is either
    an ANSI escape sequence or the empty string (colours disabled).
    """

    bold: str = ""
    dim: str = ""
    reset: str = ""
    red: str = ""
    green: str = ""
    yellow: str = ""
    blue: str = ""
    magenta: str = ""
    cyan: str = ""


_NO_COLOR_PALETTE = _Palette()
_ANSI_PALETTE = _Palette(
    bold="\033[1m",
    dim="\033[2m",
    reset="\033[0m",
    red="\033[31m",
    green="\033[32m",
    yellow="\033[33m",
    blue="\033[34m",
    magenta="\033[35m",
    cyan="\033[36m",
)


def _resolve_palette(stream: IO[str]) -> _Palette:
    """Decide whether to emit ANSI codes for `stream` (mirrors tail.sh)."""
    if os.environ.get("NO_COLOR"):
        return _NO_COLOR_PALETTE
    try:
        if not stream.isatty():
            return _NO_COLOR_PALETTE
    except (AttributeError, ValueError):
        return _NO_COLOR_PALETTE
    return _ANSI_PALETTE


def _wrap(text: str, *codes: str, reset: str) -> str:
    """Wrap text in ANSI codes, terminating with `reset`.

    When every code is empty (NO_COLOR palette), returns text unchanged so
    test assertions against literal strings continue to hold.
    """
    if not any(codes):
        return text
    return f"{''.join(codes)}{text}{reset}"


# ---------------------------------------------------------------------------
# JSON renderers
# ---------------------------------------------------------------------------


def _truncate(value: object, limit: int = 300) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "..."


def _render_assistant(
    content: object,
    *,
    show_tools: bool,
    show_system: bool,
    palette: _Palette,
) -> list[str]:
    """Render an assistant message; mirrors bash ASSISTANT/TOOL_CALL branches.

    Note: bash tail.sh has a latent bug — when jq emits multiple records for
    a multi-part assistant message, the shell only inspects the first
    record's type (cut -d'|' -f1 on the multiline string), silently dropping
    the rest. Python iterates every part, so multi-part messages always
    render every text/tool_use/thinking entry.
    """
    parts = (
        [content]
        if isinstance(content, str)
        else (content if isinstance(content, list) else [])
    )
    text_parts: list[str] = []
    extras: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                text_parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                text_parts.append(text)
        elif ptype == "tool_use" and show_tools:
            name = part.get("name", "?")
            inp = part.get("input", "")
            extras.append(
                _wrap(f"  [TOOL] {name}", palette.yellow, reset=palette.reset)
            )
            extras.append(
                _wrap(f"  {_truncate(inp, 200)}", palette.dim, reset=palette.reset)
            )
        elif ptype == "thinking" and show_system:
            thought = part.get("thinking", "")
            if thought:
                extras.append(
                    _wrap(
                        f"  (thinking) {_truncate(thought, 200)}",
                        palette.dim,
                        reset=palette.reset,
                    )
                )

    out: list[str] = []
    if text_parts:
        out.append("")
        out.append(
            _wrap("<<< ASSISTANT", palette.bold, palette.green, reset=palette.reset)
        )
        for text in text_parts:
            out.append(_wrap(text, palette.green, reset=palette.reset))
    out.extend(extras)
    return out


def _render_user(
    content: object,
    *,
    show_tools: bool,
    assistant_only: bool,
    palette: _Palette,
) -> list[str]:
    """Render a user message; mirrors bash USER/RESULT branches.

    User text is always shown (unless --assistant). tool_result parts only
    appear with --tools, mirroring bash's SHOW_TOOLS gate.
    """
    parts = (
        [content]
        if isinstance(content, str)
        else (content if isinstance(content, list) else [])
    )
    text_parts: list[str] = []
    extras: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                text_parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                text_parts.append(text)
        elif ptype == "tool_result" and show_tools:
            result = part.get("content", "")
            if isinstance(result, list):
                result = " ".join(
                    p.get("text", "") for p in result if isinstance(p, dict)
                )
            extras.append(
                _wrap(
                    f"  [result] {_truncate(result)}", palette.cyan, reset=palette.reset
                )
            )

    out: list[str] = []
    if text_parts and not assistant_only:
        out.append("")
        out.append(_wrap(">>> USER", palette.bold, palette.blue, reset=palette.reset))
        for text in text_parts:
            out.append(_wrap(text, palette.blue, reset=palette.reset))
    out.extend(extras)
    return out


def _render_object(
    obj: dict,
    *,
    show_tools: bool,
    show_system: bool,
    assistant_only: bool,
    palette: _Palette,
) -> list[str]:
    kind = obj.get("type")
    if kind == "ortus.codegraph":
        return [_render_codegraph_event(obj, palette)]

    if kind == "ortus.verdict":
        decision = str(obj.get("decision", "unknown")).upper()
        digest = str(obj.get("candidate_hash", ""))[:12]
        reason = str(obj.get("reason", "")).strip()
        colour = palette.green if decision == "PASS" else palette.red
        rendered = f"[VERDICT] {decision} candidate={digest}"
        if reason:
            rendered += f" — {_truncate(reason, 160)}"
        return [_wrap(rendered, colour, reset=palette.reset)]
    if kind == "system":
        subtype = obj.get("subtype", "?")
        if subtype == "init":
            session = obj.get("session_id", "?")
            return [
                "",
                _wrap(
                    "=== NEW SESSION ===",
                    palette.bold,
                    palette.magenta,
                    reset=palette.reset,
                ),
                _wrap(session, palette.magenta, reset=palette.reset),
            ]
        if show_system:
            return [_wrap(f"[SYS] {subtype}", palette.dim, reset=palette.reset)]
        return []
    if kind == "assistant":
        return _render_assistant(
            obj.get("message", {}).get("content"),
            show_tools=show_tools,
            show_system=show_system,
            palette=palette,
        )
    if kind == "user":
        return _render_user(
            obj.get("message", {}).get("content"),
            show_tools=show_tools,
            assistant_only=assistant_only,
            palette=palette,
        )
    if kind == "tool_use":
        if not show_tools:
            return []
        name = obj.get("name", "?")
        inp = obj.get("input", "")
        return [
            _wrap(f"  [TOOL] {name}", palette.yellow, reset=palette.reset),
            _wrap(f"  {_truncate(inp, 200)}", palette.dim, reset=palette.reset),
        ]
    if kind == "result":
        # Top-level claude session-end result. Bash tail.sh shows
        # "  [RESULT] tool: subtype" (cyan, or red on subtype=error) plus a
        # dim body line. When neither tool nor subtype is set (legacy tests,
        # minimal fixtures), fall back to plain "[result] body".
        tool = obj.get("tool")
        subtype = obj.get("subtype")
        body = obj.get("result", obj.get("error", ""))
        # Token usage: the same information class the Codex branch renders from
        # turn.completed, in claude's field names (ortus-iwac / M3). Emitted
        # unconditionally so it survives without --tools, as [USAGE] does there.
        usage = obj.get("usage")
        head: list[str] = []
        if isinstance(usage, dict):
            head.append(
                _wrap(
                    "  [USAGE] input={} cached={} output={}".format(
                        usage.get("input_tokens", 0),
                        usage.get("cache_read_input_tokens", 0),
                        usage.get("output_tokens", 0),
                    ),
                    palette.cyan,
                    reset=palette.reset,
                )
            )
        if subtype == "error":
            header = _wrap(
                f"  [RESULT] {tool or 'result'}: ERROR",
                palette.red,
                reset=palette.reset,
            )
            return head + [
                header,
                _wrap(f"  {_truncate(body, 200)}", palette.dim, reset=palette.reset),
            ]
        if tool or subtype:
            header = _wrap(
                f"  [RESULT] {tool or 'result'}: {subtype or 'ok'}",
                palette.cyan,
                reset=palette.reset,
            )
            return head + [
                header,
                _wrap(f"  {_truncate(body, 200)}", palette.dim, reset=palette.reset),
            ]
        return head + [_wrap(f"[result] {body}", palette.cyan, reset=palette.reset)]
    return []


# ---------------------------------------------------------------------------
# Codex `codex exec --json` decoder (FR-007)
# ---------------------------------------------------------------------------
#
# Codex emits JSON Lines with a flat typed envelope rather than claude's
# nested message shape. The event vocabulary is pinned by the Q2 spike
# (ortus-l75g) and its fixtures live at tests/fixtures/codex-exec-events*.jsonl:
#
#   {"type":"thread.started","thread_id":...}
#   {"type":"turn.started"}
#   {"type":"turn.completed","usage":{input_tokens,cached_input_tokens,
#                                     output_tokens,reasoning_output_tokens}}
#   {"type":"turn.failed","error":{"message":...}}
#   {"type":"error","message":...}
#   {"type":"item.started"|"item.completed","item":{"id","type",...}}
#
# item.type is one of: agent_message (assistant text), reasoning (thinking),
# command_execution (command/aggregated_output/exit_code/status), todo_list,
# error. Every field below is read by typed path — never by grepping free
# text — so a schema change surfaces as a missing render, not a wrong one.

CODEX_DECODE_ERROR_PREFIX = "!!! CODEX DECODE ERROR"


def _codex_decode_error(reason: str, line: str, palette: _Palette) -> str:
    """Loud, non-silent diagnostic for an event the decoder cannot read."""
    excerpt = line if len(line) <= 200 else line[:200] + "..."
    return _wrap(
        f"{CODEX_DECODE_ERROR_PREFIX}: {reason}: {excerpt}",
        palette.bold,
        palette.red,
        reset=palette.reset,
    )


def _render_codex_item(
    item: dict,
    *,
    started: bool,
    show_tools: bool,
    show_system: bool,
    palette: _Palette,
) -> list[str]:
    """Render one `item.started` / `item.completed` payload.

    command_execution is rendered twice by design: the `started` event is the
    tool *call* (mirrors claude's assistant tool_use) and the `completed`
    event is the tool *result* (mirrors claude's user tool_result).
    """
    itype = item.get("type")

    if itype == "agent_message":
        if started:
            return []  # text only lands on completion
        text = item.get("text", "")
        if not text:
            return []
        return [
            "",
            _wrap("<<< ASSISTANT", palette.bold, palette.green, reset=palette.reset),
            _wrap(text, palette.green, reset=palette.reset),
        ]

    if itype == "reasoning":
        if started or not show_system:
            return []
        text = item.get("text", "")
        if not text:
            return []
        return [
            _wrap(
                f"  (thinking) {_truncate(text, 200)}", palette.dim, reset=palette.reset
            )
        ]

    if itype == "command_execution":
        if not show_tools:
            return []
        if started:
            return [
                _wrap(
                    "  [TOOL] command_execution", palette.yellow, reset=palette.reset
                ),
                _wrap(
                    f"  {_truncate(item.get('command', ''), 200)}",
                    palette.dim,
                    reset=palette.reset,
                ),
            ]
        status = item.get("status", "?")
        exit_code = item.get("exit_code")
        body = str(item.get("aggregated_output", "")).rstrip("\n")
        if status == "failed" or (exit_code is not None and exit_code != 0):
            header = _wrap(
                f"  [RESULT] command_execution: ERROR (exit {exit_code})",
                palette.red,
                reset=palette.reset,
            )
        else:
            header = _wrap(
                f"  [RESULT] command_execution: {status}",
                palette.cyan,
                reset=palette.reset,
            )
        if not body:
            return [header]
        return [
            header,
            _wrap(f"  {_truncate(body, 200)}", palette.dim, reset=palette.reset),
        ]

    if itype == "todo_list":
        if not show_system:
            return []
        entries = item.get("items", [])
        done = sum(1 for e in entries if isinstance(e, dict) and e.get("completed"))
        out = [
            _wrap(f"  [TODO] {done}/{len(entries)}", palette.dim, reset=palette.reset)
        ]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            mark = "x" if entry.get("completed") else " "
            out.append(
                _wrap(
                    f"    [{mark}] {entry.get('text', '')}",
                    palette.dim,
                    reset=palette.reset,
                )
            )
        return out

    if itype == "error":
        if started:
            return []
        return [
            _wrap(
                f"  [ERROR] {item.get('message', '')}", palette.red, reset=palette.reset
            )
        ]

    if show_system:
        return [_wrap(f"[SYS] item.{itype}", palette.dim, reset=palette.reset)]
    return []


def _render_codex_object(
    obj: dict,
    *,
    show_tools: bool,
    show_system: bool,
    palette: _Palette,
) -> list[str]:
    kind = obj.get("type")

    if kind == "ortus.codegraph":
        return [_render_codegraph_event(obj, palette)]

    if kind == "ortus.verdict":
        decision = str(obj.get("decision", "unknown")).upper()
        digest = str(obj.get("candidate_hash", ""))[:12]
        reason = str(obj.get("reason", "")).strip()
        colour = palette.green if decision == "PASS" else palette.red
        rendered = f"[VERDICT] {decision} candidate={digest}"
        if reason:
            rendered += f" — {_truncate(reason, 160)}"
        return [_wrap(rendered, colour, reset=palette.reset)]

    if kind == "thread.started":
        return [
            "",
            _wrap(
                "=== NEW SESSION ===",
                palette.bold,
                palette.magenta,
                reset=palette.reset,
            ),
            _wrap(str(obj.get("thread_id", "?")), palette.magenta, reset=palette.reset),
        ]

    if kind == "turn.completed":
        usage = obj.get("usage") or {}
        return [
            _wrap(
                "  [USAGE] input={} cached={} output={} reasoning={}".format(
                    usage.get("input_tokens", 0),
                    usage.get("cached_input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("reasoning_output_tokens", 0),
                ),
                palette.cyan,
                reset=palette.reset,
            )
        ]

    if kind == "turn.failed":
        message = (obj.get("error") or {}).get("message", "")
        return [_wrap(f"  [TURN FAILED] {message}", palette.red, reset=palette.reset)]

    if kind == "error":
        return [
            _wrap(
                f"  [ERROR] {obj.get('message', '')}", palette.red, reset=palette.reset
            )
        ]

    if kind in ("item.started", "item.completed"):
        item = obj.get("item")
        if not isinstance(item, dict):
            return []
        return _render_codex_item(
            item,
            started=kind == "item.started",
            show_tools=show_tools,
            show_system=show_system,
            palette=palette,
        )

    if show_system:
        return [_wrap(f"[SYS] {kind}", palette.dim, reset=palette.reset)]
    return []


def _render_codegraph_event(obj: dict, palette: _Palette) -> str:
    """Render normalized lifecycle records at every verbosity level."""
    phase = obj.get("phase", "?")
    if obj.get("kind") == "handshake":
        state = "succeeded" if obj.get("success") else "failed"
        line = f"[CODEGRAPH] {phase} child handshake {state}"
        if obj.get("reason"):
            line += f": {obj['reason']}"
    elif obj.get("kind") == "query":
        state = "ok" if obj.get("success") else "error"
        hit = obj.get("hit")
        hit_label = "hit" if hit is True else "miss" if hit is False else "unknown"
        line = (
            f"[CODEGRAPH] {phase} {obj.get('tool', '?')} "
            f"{obj.get('query', 'query')} — {state}/{hit_label}"
        )
    else:
        line = (
            f"[CODEGRAPH] {phase} summary: available={obj.get('available', False)} "
            f"queries={obj.get('query_count', 0)} freshness={obj.get('freshness', 'unknown')}"
        )
        fallbacks = obj.get("fallbacks") or []
        if fallbacks:
            line += f" fallback={'; '.join(str(value) for value in fallbacks[:3])}"
    return _wrap(line, palette.cyan, reset=palette.reset)


def _format_codex_line(
    line: str,
    *,
    show_tools: bool,
    show_system: bool,
    palette: _Palette = _NO_COLOR_PALETTE,
) -> str | None:
    """Render one `codex exec --json` line; returns None when filtered out.

    A line that looks like an event but cannot be decoded (truncated write,
    schema drift) is reported loudly rather than dropped — silent skipping is
    how a broken decoder masquerades as a quiet run.
    """
    line = line.rstrip("\n")
    if not line:
        return None
    if not line.startswith("{"):
        return _render_plain(line, palette)
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        return _codex_decode_error(str(exc), line, palette)
    if not isinstance(obj, dict):
        return _codex_decode_error("event is not a JSON object", line, palette)
    if not obj.get("type"):
        return _codex_decode_error("event has no `type` field", line, palette)
    pieces = _render_codex_object(
        obj, show_tools=show_tools, show_system=show_system, palette=palette
    )
    if not pieces:
        return None
    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# opencode `opencode run --format json` decoder
# ---------------------------------------------------------------------------
#
# opencode emits JSON Lines, one per message part, in the shape the Q1 spike
# recorded from opencode 1.18.27 (tests/fixtures/opencode-run-events.jsonl):
#
#   {"type":"step_start","timestamp":<epoch ms>,"sessionID":...,
#    "part":{"type":"step-start",...}}
#   {"type":"text",...,"part":{"type":"text","text":...}}
#   {"type":"tool_use",...,"part":{"type":"tool","tool":<name>,"callID":...,
#    "state":{"status","input","output","title","time"}}}
#   {"type":"step_finish",...,"part":{"type":"step-finish",
#    "reason":"stop"|"tool-calls","tokens":{"total","input","output",
#    "reasoning","cache":{"read","write"}},"cost"}}
#
# A tool_use is the call and its result in one event, so it renders as the
# codex command pair does: the call under [TOOL] (or [MCP] for a tool opencode
# runs on a registered MCP server, which it names `<server>_<tool>`) and the
# outcome under [RESULT]. Every field is read by typed path, never by
# grepping free text, so a schema change surfaces as a missing render.

OPENCODE_DECODE_ERROR_PREFIX = "!!! OPENCODE DECODE ERROR"


def _opencode_decode_error(reason: str, line: str, palette: _Palette) -> str:
    """Loud, non-silent diagnostic for an event the decoder cannot read."""
    excerpt = line if len(line) <= 200 else line[:200] + "..."
    return _wrap(
        f"{OPENCODE_DECODE_ERROR_PREFIX}: {reason}: {excerpt}",
        palette.bold,
        palette.red,
        reset=palette.reset,
    )


def _render_opencode_tool(part: dict, *, palette: _Palette) -> list[str]:
    """Render one tool part: the call, then its outcome."""
    name = str(part.get("tool") or "tool")
    label = "[MCP]" if _opencode_mcp_server(name) else "[TOOL]"
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    out = [_wrap(f"  {label} {name}", palette.yellow, reset=palette.reset)]
    raw = state.get("input")
    if raw not in (None, "", {}):
        out.append(_wrap(f"  {_truncate(raw, 200)}", palette.dim, reset=palette.reset))
    status = str(state.get("status") or "?")
    if status == "error":
        out.append(
            _wrap(f"  [RESULT] {name}: ERROR", palette.red, reset=palette.reset)
        )
        body = str(state.get("error") or "")
    else:
        out.append(
            _wrap(f"  [RESULT] {name}: {status}", palette.cyan, reset=palette.reset)
        )
        output = state.get("output")
        body = "" if output is None else _truncate(output, 200)
    body = body.rstrip("\n")
    if body:
        out.append(_wrap(f"  {_truncate(body, 200)}", palette.dim, reset=palette.reset))
    return out


def _render_opencode_object(
    obj: dict,
    *,
    show_tools: bool,
    show_system: bool,
    palette: _Palette,
) -> list[str]:
    kind = obj.get("type")

    if kind == "ortus.codegraph":
        return [_render_codegraph_event(obj, palette)]

    if kind == "ortus.verdict":
        decision = str(obj.get("decision", "unknown")).upper()
        digest = str(obj.get("candidate_hash", ""))[:12]
        reason = str(obj.get("reason", "")).strip()
        colour = palette.green if decision == "PASS" else palette.red
        rendered = f"[VERDICT] {decision} candidate={digest}"
        if reason:
            rendered += f" — {_truncate(reason, 160)}"
        return [_wrap(rendered, colour, reset=palette.reset)]

    part = obj.get("part")
    if not isinstance(part, dict):
        if show_system:
            return [_wrap(f"[SYS] {kind}", palette.dim, reset=palette.reset)]
        return []

    if kind == "text":
        text = part.get("text", "")
        if not text:
            return []
        return [
            "",
            _wrap("<<< ASSISTANT", palette.bold, palette.green, reset=palette.reset),
            _wrap(str(text), palette.green, reset=palette.reset),
        ]

    if kind == "tool_use":
        if not show_tools:
            return []
        return _render_opencode_tool(part, palette=palette)

    if kind == "step_finish":
        reason = str(part.get("reason") or "?")
        # The stop step is the turn's end and is always shown, as codex's
        # turn.completed is; the steps between tool calls are system noise.
        if reason != OPENCODE_STEP_STOP and not show_system:
            return []
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        return [
            _wrap(
                "  [USAGE] input={} cached={} output={} reasoning={} reason={}".format(
                    tokens.get("input", 0),
                    cache.get("read", 0),
                    tokens.get("output", 0),
                    tokens.get("reasoning", 0),
                    reason,
                ),
                palette.cyan,
                reset=palette.reset,
            )
        ]

    if show_system:
        # step_start, and any part kind this decoder was not written for
        # (a reasoning part from a thinking model, say): named, never guessed.
        return [_wrap(f"[SYS] {kind}", palette.dim, reset=palette.reset)]
    return []


def _format_opencode_line(
    line: str,
    *,
    show_tools: bool,
    show_system: bool,
    palette: _Palette = _NO_COLOR_PALETTE,
) -> str | None:
    """Render one `opencode run --format json` line; None when filtered out.

    A line that looks like an event but cannot be decoded (truncated write,
    schema drift) is reported loudly rather than dropped, as the codex
    decoder does — silent skipping is how a broken decoder masquerades as a
    quiet run.
    """
    line = line.rstrip("\n")
    if not line:
        return None
    if not line.startswith("{"):
        return _render_plain(line, palette)
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        return _opencode_decode_error(str(exc), line, palette)
    if not isinstance(obj, dict):
        return _opencode_decode_error("event is not a JSON object", line, palette)
    if not obj.get("type"):
        return _opencode_decode_error("event has no `type` field", line, palette)
    pieces = _render_opencode_object(
        obj, show_tools=show_tools, show_system=show_system, palette=palette
    )
    if not pieces:
        return None
    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# Non-JSON line colouring (mirrors bash format_line non-JSON branch)
# ---------------------------------------------------------------------------


_BANNER_RE = re.compile(r"^===")
_INFO_RE = re.compile(r"^(Processing:|Found)")
_ERROR_RE = re.compile(r"(error|Error|ERROR)")
_SUCCESS_RE = re.compile(r"(success|Success|completed)")
_GRIND_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} ")


def _render_plain(line: str, palette: _Palette) -> str:
    if not line:
        return line
    if _GRIND_RE.match(line):
        return _wrap(line, palette.bold, palette.yellow, reset=palette.reset)
    if _BANNER_RE.search(line):
        return "\n" + _wrap(line, palette.bold, palette.cyan, reset=palette.reset)
    if _INFO_RE.search(line):
        return _wrap(line, palette.cyan, reset=palette.reset)
    if _ERROR_RE.search(line):
        return _wrap(line, palette.red, reset=palette.reset)
    if _SUCCESS_RE.search(line):
        return _wrap(line, palette.green, reset=palette.reset)
    return _wrap(line, palette.dim, reset=palette.reset)


@dataclass
class _GrokCoalesce:
    """Buffer consecutive Grok thought/text crumbs into one paragraph."""

    kind: str | None = None
    buf: str = ""

    def flush(self, palette: _Palette) -> list[str]:
        if not self.buf:
            self.kind = None
            return []
        body = " ".join(self.buf.split())
        kind = self.kind
        self.buf = ""
        self.kind = None
        if not body:
            return []
        if kind == "thought":
            return [_wrap(f"  think  {body}", palette.dim, reset=palette.reset)]
        return [_wrap(f"  text   {body}", reset=palette.reset)]

    def append(self, kind: str, chunk: str, palette: _Palette) -> list[str]:
        out: list[str] = []
        if self.kind is not None and self.kind != kind:
            out.extend(self.flush(palette))
        self.kind = kind
        self.buf += chunk
        if "\n" in chunk:
            out.extend(self.flush(palette))
        return out


def _render_grok_object(
    obj: dict,
    *,
    grok: _GrokCoalesce,
    show_system: bool,
    palette: _Palette,
) -> list[str]:
    """Render one Grok streaming-json event; may flush a pending paragraph."""
    kind = obj.get("type")
    if kind in ("usage", "available_commands"):
        return []
    if kind in ("thought", "text"):
        data = obj.get("data")
        chunk = "" if data is None else str(data)
        return grok.append(str(kind), chunk, palette)
    if kind == "tool_call":
        # Default Grok view always includes tools (unlike Claude --tools).
        out = grok.flush(palette)
        out.append(
            _wrap(
                f"  tool   {_summarize_grok_tool(obj)}",
                palette.bold,
                palette.cyan,
                reset=palette.reset,
            )
        )
        return out
    if kind == "tool_call_update":
        status = obj.get("status") or ""
        if status == "completed":
            out = grok.flush(palette)
            out.append(_wrap("  done   tool", palette.green, reset=palette.reset))
            return out
        if status in ("failed", "error"):
            out = grok.flush(palette)
            out.append(_wrap("  fail   tool", palette.red, reset=palette.reset))
            return out
        return []
    if kind == "plan":
        out = grok.flush(palette)
        if not show_system:
            return out
        entries = obj.get("entries") or []
        if not isinstance(entries, list):
            return out
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            status = entry.get("status", "?")
            content = entry.get("content", "")
            out.append(f"  plan   [{status}] {content}")
        return out
    return []


def _join_rendered(pieces: list[str]) -> str | None:
    if not pieces:
        return None
    return "\n".join(pieces)


def _format_line(
    line: str,
    *,
    show_tools: bool,
    show_system: bool,
    assistant_only: bool = False,
    palette: _Palette = _NO_COLOR_PALETTE,
    grok: _GrokCoalesce | None = None,
) -> str | None:
    """Render one stream-json line; returns None when filtered out.

    May embed newlines when one JSON object yields multiple rendered lines
    (e.g., an assistant message with both text and tool_use parts, or a Grok
    tool_call that flushes a pending think/text paragraph).

    Pass a shared `grok` buffer (one per `_LogStream`) to coalesce consecutive
    thought/text crumbs. Without one, each Grok crumb flushes immediately.
    """
    line = line.rstrip("\n")
    if not line:
        return None
    if not line.startswith("{"):
        pieces: list[str] = []
        if grok is not None:
            pieces.extend(grok.flush(palette))
        pieces.append(_render_plain(line, palette))
        return _join_rendered(pieces)
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        pieces = []
        if grok is not None:
            pieces.extend(grok.flush(palette))
        pieces.append(_render_plain(line, palette))
        return _join_rendered(pieces)
    if _is_grok_event(obj):
        owned = grok if grok is not None else _GrokCoalesce()
        pieces = _render_grok_object(
            obj, grok=owned, show_system=show_system, palette=palette
        )
        if grok is None:
            pieces.extend(owned.flush(palette))
        return _join_rendered(pieces)
    pieces = []
    if grok is not None:
        pieces.extend(grok.flush(palette))
    pieces.extend(
        _render_object(
            obj,
            show_tools=show_tools,
            show_system=show_system,
            assistant_only=assistant_only,
            palette=palette,
        )
    )
    return _join_rendered(pieces)


@dataclass
class _LogStream:
    path: Path
    pos: int = 0
    #: The stream's first read has happened, so the attach-time history cap
    #: no longer applies; everything after this point is live follow output.
    attached: bool = False
    grok: _GrokCoalesce = field(default_factory=_GrokCoalesce)


def _json_object(line: str) -> dict | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _grok_event_kind(line: str) -> str | None:
    obj = _json_object(line)
    if obj is None or not _is_grok_event(obj):
        return None
    kind = obj.get("type")
    return str(kind) if kind else None


def _claude_has_thinking(line: str) -> bool:
    obj = _json_object(line)
    if obj is None or obj.get("type") != "assistant":
        return False
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    parts = content if isinstance(content, list) else []
    return any(isinstance(part, dict) and part.get("type") == "thinking" for part in parts)


def _grok_crumb_data(line: str) -> str | None:
    obj = _json_object(line)
    if (
        obj is None
        or not _is_grok_event(obj)
        or obj.get("type") not in ("thought", "text")
    ):
        return None
    data = obj.get("data")
    return "" if data is None else str(data)


def _expand_grok_paragraph(lines: list[str], start: int) -> int:
    """Walk `start` back to the first crumb of the current thought/text run."""
    if start >= len(lines):
        return start
    kind = _grok_event_kind(lines[start])
    if kind not in ("thought", "text"):
        if (
            kind
            in (
                "tool_call",
                "tool_call_update",
                "usage",
                "available_commands",
                "plan",
            )
            and start > 0
            and _grok_event_kind(lines[start - 1]) in ("thought", "text")
        ):
            return _expand_grok_paragraph(lines, start - 1)
        return start
    while start > 0 and _grok_event_kind(lines[start - 1]) == kind:
        prev_data = _grok_crumb_data(lines[start - 1])
        if prev_data is not None and "\n" in prev_data:
            break
        start -= 1
    return start


def _walk_attach_start(lines: list[str], start: int, *, show_system: bool) -> int:
    """Move `start` earlier so a small cap still holds the current think block."""
    n = len(lines)
    if start <= 0 or start >= n:
        return start

    dropped = frozenset({"usage", "available_commands"})
    while start > 0 and all(_grok_event_kind(line) in dropped for line in lines[start:]):
        start -= 1

    start = _expand_grok_paragraph(lines, start)

    if show_system and not any(_claude_has_thinking(line) for line in lines[start:]):
        probe = start
        while probe > 0:
            prev = lines[probe - 1]
            if _json_object(prev) is None or _grok_event_kind(prev) is not None:
                break
            probe -= 1
            if _claude_has_thinking(prev):
                start = probe
                break
    return start


def _attach_window(
    chunk_lines: list[str],
    cap: int,
    *,
    raw: bool,
    show_system: bool,
) -> tuple[list[str], int]:
    """Attach-time slice only. Follow-after-attach never calls this.

    `cap` is the operator ``--lines`` budget (must be > 0). Raw mode is a
    strict last-N raw-line slice. Decoded mode walks the skipped prefix so
    the current Grok thought/text paragraph, a think that a trailing
    ``usage`` would hide, and (when ``show_system``) the previous Claude
    assistant thinking event stay in the window.
    """
    total = len(chunk_lines)
    if cap <= 0 or total <= cap:
        return chunk_lines, 0
    start = total - cap
    if not raw:
        start = _walk_attach_start(chunk_lines, start, show_system=show_system)
    return chunk_lines[start:], start


def _newest_log(paths: Iterable[Path]) -> Path | None:
    """Newest path by the same `(mtime, name)` key `find_log` uses."""
    files = [path for path in paths if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: (_mtime(path), path.name))


def _selected_logs(logs_dir: Path, *, follow_all: bool) -> set[Path]:
    discovered = _discover_logs(logs_dir)
    if follow_all:
        return discovered
    newest = _newest_log(discovered)
    return {newest} if newest is not None else set()


def _follow(
    logs_dir: Path,
    *,
    raw: bool,
    show_tools: bool,
    show_system: bool,
    iterations: int = -1,
    out: IO[str] | None = None,
    initial_files: Optional[Iterable[Path]] = None,
    assistant_only: bool = False,
    palette: Optional[_Palette] = None,
    codex: bool = False,
    err: IO[str] | None = None,
    lines: int = DEFAULT_ATTACH_LINES,
    follow_all: bool = False,
    opencode: bool = False,
) -> None:
    """Polling tail. `iterations<0` runs forever; finite values for tests.

    `lines` caps how much existing history each stream renders at first
    attach (0 = unlimited); follow output after attach is never trimmed.
    Default follows only the newest discovered log and switches if a newer
    one appears. `follow_all` attaches every matching file. An explicit
    `initial_files` set is followed exactly and is not remapped.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    if palette is None:
        palette = _resolve_palette(out)
    streams: dict[Path, _LogStream] = {}
    pinned = initial_files is not None

    if pinned:
        for p in initial_files:
            streams[p] = _LogStream(p)

    i = 0
    while iterations < 0 or i < iterations:
        if not pinned:
            selected = _selected_logs(logs_dir, follow_all=follow_all)
            for stale in list(streams):
                if stale not in selected:
                    del streams[stale]
            for p in selected:
                if p not in streams:
                    streams[p] = _LogStream(p)
                    banner = _wrap(
                        f"=== TAILING: {p.name} ===",
                        palette.bold,
                        palette.magenta,
                        reset=palette.reset,
                    )
                    out.write(f"{banner}\n")
                    out.flush()
        for stream in streams.values():
            if not stream.path.is_file():
                continue
            with stream.path.open("r", encoding="utf-8") as fh:
                fh.seek(stream.pos)
                chunk = fh.read()
                stream.pos = fh.tell()
            first_read = not stream.attached
            stream.attached = True
            chunk_lines: list[str] = []
            if chunk:
                chunk_lines = chunk.splitlines()
                # The cap selects the attach window before rendering and only
                # on the first read; position accounting above stays
                # byte-accurate, so follow reads are untouched.
                if first_read and lines > 0:
                    chunk_lines, skipped = _attach_window(
                        chunk_lines, lines, raw=raw, show_system=show_system
                    )
                    if skipped > 0:
                        notice = _wrap(
                            f"=== SKIPPED {skipped} earlier lines: {stream.path.name} "
                            "(use --lines 0 for full history) ===",
                            palette.bold,
                            palette.magenta,
                            reset=palette.reset,
                        )
                        out.write(f"{notice}\n")
                for line in chunk_lines:
                    if raw:
                        out.write(line + "\n")
                        continue
                    if codex:
                        rendered = _format_codex_line(
                            line,
                            show_tools=show_tools,
                            show_system=show_system,
                            palette=palette,
                        )
                        if rendered is not None and CODEX_DECODE_ERROR_PREFIX in rendered:
                            err.write(rendered + "\n")
                            err.flush()
                    elif opencode:
                        rendered = _format_opencode_line(
                            line,
                            show_tools=show_tools,
                            show_system=show_system,
                            palette=palette,
                        )
                        if rendered is not None and OPENCODE_DECODE_ERROR_PREFIX in rendered:
                            err.write(rendered + "\n")
                            err.flush()
                    else:
                        rendered = _format_line(
                            line,
                            show_tools=show_tools,
                            show_system=show_system,
                            assistant_only=assistant_only,
                            palette=palette,
                            grok=stream.grok,
                        )
                    if rendered is not None:
                        out.write(rendered + "\n")
            if not raw:
                last_iter = iterations >= 0 and i + 1 >= iterations
                if (chunk_lines and first_read) or last_iter:
                    leftover = stream.grok.flush(palette)
                    if leftover:
                        out.write("\n".join(leftover) + "\n")
            out.flush()
        if iterations < 0 or i + 1 < iterations:
            time.sleep(POLL_SECONDS)
        i += 1


def _discover_logs(logs_dir: Path) -> set[Path]:
    if not logs_dir.is_dir():
        return set()
    out: set[Path] = set()
    for prefix in PREFIXES:
        out.update(p for p in logs_dir.glob(f"{prefix}*.log") if p.is_file())
    return out


def tail(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Emit log lines verbatim (no stream-json filtering)."
    ),
    tools: bool = typer.Option(
        False,
        "--tools",
        "-t",
        help="Include tool_use and tool_result entries (assistant calls + user results).",
    ),
    system: bool = typer.Option(
        False,
        "--system",
        "-s",
        help="Include non-init system events (hook_started, hook_response, thinking, ...).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Equivalent to --tools --system; superset of every category.",
    ),
    assistant: bool = typer.Option(
        False,
        "--assistant",
        "-a",
        help="Show assistant messages only (suppress USER blocks; mirrors tail.sh -a).",
    ),
    codex: bool = typer.Option(
        False,
        "--codex",
        help="Compatibility shorthand for --backend codex.",
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="Log backend (claude|codex|grok|local|opencode); defaults from .ortusrc.",
    ),
    lines: int = typer.Option(
        DEFAULT_ATTACH_LINES,
        "--lines",
        "-n",
        min=0,
        help=(
            "Lines of existing history to render per log at attach "
            "(0 = unlimited; follow output is never trimmed)."
        ),
    ),
    follow_all: bool = typer.Option(
        False,
        "--all",
        help="Follow every matching log. Default follows only the newest.",
    ),
) -> None:
    """Tail the newest orchestrator log (grind-*, goal-*, ralph-*, plan-*).

    Default follows only the newest matching file and switches if a newer
    one appears. Use --all to follow every matching log.

    Always shown: assistant/user text, system:init banners, top-level results,
    plain-text banners. Use -t to add tool calls/results, -s to add other
    system events, -v for both, -a to hide user blocks.

    Colours mirror ortus/tail.sh: bold green for assistant, bold blue for
    user, bold magenta for session banners, yellow for tool calls, cyan for
    results, dim for system. Set NO_COLOR=1 (https://no-color.org/) or pipe
    to a non-tty to disable.
    """
    target = resolve_repo(repo)
    try:
        resolved_backend = resolve_backend(
            "codex" if codex else backend,
            repo=target,
        )
    except BackendError as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
    logs_dir = target / "logs"
    if verbose:
        tools = True
        system = True
    _follow(
        logs_dir,
        raw=raw,
        follow_all=follow_all,
        show_tools=tools,
        show_system=system,
        assistant_only=assistant,
        # An opencode run writes opencode events and takes its own decoder;
        # `local` is opencode under its older name, so its log is one too.
        codex=resolved_backend == "codex",
        opencode=resolved_backend in LOCAL_TABLE_BACKENDS,
        lines=lines,
    )
