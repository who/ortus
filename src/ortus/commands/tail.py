"""ortus tail <repo> — follow logs/{grind,goal,ralph}-*.log (idzn.4).

Strictly read-only (NFR-006). Default formatting filters claude stream-json
into human-readable turn boundaries; --raw emits lines verbatim. Polls the
logs/ directory every 1s for new files matching the back-compat prefixes.

Verbosity contract (must stay a SUPERSET of legacy ortus/tail.sh):

    Always shown (any verbosity):
      - non-JSON lines (timestamp banners, etc.)
      - assistant text content
      - user text content
      - system:init events (rendered as "=== NEW SESSION ===" banner)
      - top-level result events

    --tools / -t:
      - assistant tool_use entries (e.g., "  ↳ Bash(ls)")
      - user tool_result entries

    --system / -s:
      - all other system subtypes (hook_started, hook_response, ...)

    --verbose / -v:  equivalent to --tools --system
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable, Optional

import typer

from ortus.core.repo import resolve_repo

PREFIXES = ("grind-", "goal-", "ralph-")
POLL_SECONDS = 1.0


def _discover_logs(logs_dir: Path) -> set[Path]:
    if not logs_dir.is_dir():
        return set()
    out: set[Path] = set()
    for prefix in PREFIXES:
        out.update(p for p in logs_dir.glob(f"{prefix}*.log") if p.is_file())
    return out


def _truncate(value: object, limit: int = 300) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "..."


def _render_assistant(content: object, *, show_tools: bool, show_system: bool) -> list[str]:
    if isinstance(content, str):
        return [f"  {content}"] if content else []
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                out.append(f"  {text}")
        elif ptype == "tool_use":
            if show_tools:
                name = part.get("name", "?")
                inp = part.get("input", "")
                out.append(f"  ↳ {name}({_truncate(inp, 200)})")
        elif ptype == "thinking":
            if show_system:
                thought = part.get("thinking", "")
                out.append(f"  (thinking) {_truncate(thought, 200)}")
    return out


def _render_user(content: object, *, show_tools: bool) -> list[str]:
    if isinstance(content, str):
        return [f">>> USER: {content}"] if content else []
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                out.append(f">>> USER: {text}")
        elif ptype == "tool_result":
            if show_tools:
                result = part.get("content", "")
                if isinstance(result, list):
                    result = " ".join(
                        p.get("text", "") for p in result if isinstance(p, dict)
                    )
                out.append(f"[result] {_truncate(result)}")
    return out


def _render_object(obj: dict, *, show_tools: bool, show_system: bool) -> list[str]:
    kind = obj.get("type")
    if kind == "system":
        subtype = obj.get("subtype", "?")
        if subtype == "init":
            session = obj.get("session_id", "?")
            return [f"=== NEW SESSION === ({session})"]
        if show_system:
            return [f"[system:{subtype}]"]
        return []
    if kind == "assistant":
        return _render_assistant(
            obj.get("message", {}).get("content"),
            show_tools=show_tools,
            show_system=show_system,
        )
    if kind == "user":
        return _render_user(
            obj.get("message", {}).get("content"),
            show_tools=show_tools,
        )
    if kind == "tool_use":
        if not show_tools:
            return []
        return [f"  ↳ {obj.get('name', '?')}({_truncate(obj.get('input', ''), 200)})"]
    if kind == "result":
        return [f"[result] {obj.get('result', '')}"]
    return []


def _format_line(line: str, *, show_tools: bool, show_system: bool) -> str | None:
    """Render one stream-json line into a human-readable string; None to skip.

    May embed newlines when one JSON object yields multiple rendered lines
    (e.g., an assistant message with both text and tool_use parts).
    """
    line = line.rstrip("\n")
    if not line:
        return None
    if not line.startswith("{"):
        return line  # already human-readable (e.g., timestamp banner)
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    pieces = _render_object(obj, show_tools=show_tools, show_system=show_system)
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
) -> None:
    """Polling tail. `iterations<0` runs forever; finite values for tests."""
    out = out or sys.stdout
    streams: dict[Path, _LogStream] = {}
    seen: set[Path] = set()

    if initial_files is not None:
        for p in initial_files:
            streams[p] = _LogStream(p)
            seen.add(p)

    i = 0
    while iterations < 0 or i < iterations:
        # Pick up new files.
        for p in _discover_logs(logs_dir):
            if p not in seen:
                seen.add(p)
                streams[p] = _LogStream(p)
                out.write(f"==> {p.name} <==\n")
                out.flush()
        # Read new bytes from each.
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
                    rendered = _format_line(line, show_tools=show_tools, show_system=show_system)
                    if rendered is not None:
                        out.write(rendered + "\n")
            out.flush()
        if iterations < 0 or i + 1 < iterations:
            time.sleep(POLL_SECONDS)
        i += 1


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
        help="Include non-init system events (hook_started, hook_response, ...).",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Equivalent to --tools --system; superset of every category.",
    ),
) -> None:
    """Tail orchestrator log files (grind-*, goal-*, ralph-*).

    Always shown: assistant/user text, system:init banners, top-level results,
    plain-text banners. Use -t to add tool calls/results, -s to add other
    system events, -v for both.
    """
    target = resolve_repo(repo)
    logs_dir = target / "logs"
    if verbose:
        tools = True
        system = True
    _follow(logs_dir, raw=raw, show_tools=tools, show_system=system)
