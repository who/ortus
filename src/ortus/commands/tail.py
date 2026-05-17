"""ortus tail <repo> — follow logs/{grind,goal,ralph,plan}-*.log (idzn.4).

Strictly read-only (NFR-006). Default formatting filters claude stream-json
into human-readable turn boundaries; --raw emits lines verbatim. Polls the
logs/ directory every 1s for new files matching the back-compat prefixes.

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
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable, Optional

import typer

from ortus.core.repo import resolve_repo

PREFIXES = ("grind-", "goal-", "ralph-", "plan-")
POLL_SECONDS = 1.0


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
    parts = [content] if isinstance(content, str) else (content if isinstance(content, list) else [])
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
            extras.append(_wrap(f"  [TOOL] {name}", palette.yellow, reset=palette.reset))
            extras.append(_wrap(f"  {_truncate(inp, 200)}", palette.dim, reset=palette.reset))
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
        out.append(_wrap("<<< ASSISTANT", palette.bold, palette.green, reset=palette.reset))
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
    parts = [content] if isinstance(content, str) else (content if isinstance(content, list) else [])
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
                result = " ".join(p.get("text", "") for p in result if isinstance(p, dict))
            extras.append(_wrap(f"  [result] {_truncate(result)}", palette.cyan, reset=palette.reset))

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
    if kind == "system":
        subtype = obj.get("subtype", "?")
        if subtype == "init":
            session = obj.get("session_id", "?")
            return [
                "",
                _wrap("=== NEW SESSION ===", palette.bold, palette.magenta, reset=palette.reset),
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
        if subtype == "error":
            header = _wrap(
                f"  [RESULT] {tool or 'result'}: ERROR", palette.red, reset=palette.reset
            )
            return [header, _wrap(f"  {_truncate(body, 200)}", palette.dim, reset=palette.reset)]
        if tool or subtype:
            header = _wrap(
                f"  [RESULT] {tool or 'result'}: {subtype or 'ok'}",
                palette.cyan,
                reset=palette.reset,
            )
            return [header, _wrap(f"  {_truncate(body, 200)}", palette.dim, reset=palette.reset)]
        return [_wrap(f"[result] {body}", palette.cyan, reset=palette.reset)]
    return []


# ---------------------------------------------------------------------------
# Non-JSON line colouring (mirrors bash format_line non-JSON branch)
# ---------------------------------------------------------------------------


_BANNER_RE = re.compile(r"^===")
_INFO_RE = re.compile(r"^(Processing:|Found)")
_ERROR_RE = re.compile(r"(error|Error|ERROR)")
_SUCCESS_RE = re.compile(r"(success|Success|completed)")


def _render_plain(line: str, palette: _Palette) -> str:
    if not line:
        return line
    if _BANNER_RE.search(line):
        return "\n" + _wrap(line, palette.bold, palette.cyan, reset=palette.reset)
    if _INFO_RE.search(line):
        return _wrap(line, palette.cyan, reset=palette.reset)
    if _ERROR_RE.search(line):
        return _wrap(line, palette.red, reset=palette.reset)
    if _SUCCESS_RE.search(line):
        return _wrap(line, palette.green, reset=palette.reset)
    return _wrap(line, palette.dim, reset=palette.reset)


def _format_line(
    line: str,
    *,
    show_tools: bool,
    show_system: bool,
    assistant_only: bool = False,
    palette: _Palette = _NO_COLOR_PALETTE,
) -> str | None:
    """Render one stream-json line; returns None when filtered out.

    May embed newlines when one JSON object yields multiple rendered lines
    (e.g., an assistant message with both text and tool_use parts).
    """
    line = line.rstrip("\n")
    if not line:
        return None
    if not line.startswith("{"):
        return _render_plain(line, palette)
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return _render_plain(line, palette)
    pieces = _render_object(
        obj,
        show_tools=show_tools,
        show_system=show_system,
        assistant_only=assistant_only,
        palette=palette,
    )
    if not pieces:
        return None
    return "\n".join(pieces)


@dataclass
class _LogStream:
    path: Path
    pos: int = 0


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
) -> None:
    """Polling tail. `iterations<0` runs forever; finite values for tests."""
    out = out or sys.stdout
    if palette is None:
        palette = _resolve_palette(out)
    streams: dict[Path, _LogStream] = {}
    seen: set[Path] = set()

    if initial_files is not None:
        for p in initial_files:
            streams[p] = _LogStream(p)
            seen.add(p)

    i = 0
    while iterations < 0 or i < iterations:
        for p in _discover_logs(logs_dir):
            if p not in seen:
                seen.add(p)
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
            if not chunk:
                continue
            for line in chunk.splitlines():
                if raw:
                    out.write(line + "\n")
                else:
                    rendered = _format_line(
                        line,
                        show_tools=show_tools,
                        show_system=show_system,
                        assistant_only=assistant_only,
                        palette=palette,
                    )
                    if rendered is not None:
                        out.write(rendered + "\n")
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
    raw: bool = typer.Option(False, "--raw", help="Emit log lines verbatim (no stream-json filtering)."),
    tools: bool = typer.Option(
        False, "--tools", "-t",
        help="Include tool_use and tool_result entries (assistant calls + user results).",
    ),
    system: bool = typer.Option(
        False, "--system", "-s",
        help="Include non-init system events (hook_started, hook_response, thinking, ...).",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Equivalent to --tools --system; superset of every category.",
    ),
    assistant: bool = typer.Option(
        False, "--assistant", "-a",
        help="Show assistant messages only (suppress USER blocks; mirrors tail.sh -a).",
    ),
) -> None:
    """Tail orchestrator log files (grind-*, goal-*, ralph-*, plan-*).

    Always shown: assistant/user text, system:init banners, top-level results,
    plain-text banners. Use -t to add tool calls/results, -s to add other
    system events, -v for both, -a to hide user blocks.

    Colours mirror ortus/tail.sh: bold green for assistant, bold blue for
    user, bold magenta for session banners, yellow for tool calls, cyan for
    results, dim for system. Set NO_COLOR=1 (https://no-color.org/) or pipe
    to a non-tty to disable.
    """
    target = resolve_repo(repo)
    logs_dir = target / "logs"
    if verbose:
        tools = True
        system = True
    _follow(
        logs_dir,
        raw=raw,
        show_tools=tools,
        show_system=system,
        assistant_only=assistant,
    )
