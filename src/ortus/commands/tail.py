"""ortus tail <repo> — follow logs/{grind,goal,ralph}-*.log (idzn.4).

Strictly read-only (NFR-006). Default formatting filters claude stream-json
into human-readable turn boundaries; --raw emits lines verbatim. Polls the
logs/ directory every 1s for new files matching the back-compat prefixes.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
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


def _format_line(line: str, *, show_tools: bool, show_system: bool) -> str | None:
    """Render one stream-json line into a human-readable string; None to skip."""
    line = line.rstrip("\n")
    if not line:
        return None
    if not line.startswith("{"):
        return line  # already human-readable (e.g., timestamp banner)
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    kind = obj.get("type")
    if kind == "system":
        if not show_system:
            return None
        return f"[system:{obj.get('subtype', '?')}]"
    if kind == "assistant":
        msg = obj.get("message", {})
        content = msg.get("content")
        if isinstance(content, str):
            return f"  {content}"
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            return f"  {' '.join(parts)}"
        return None
    if kind == "tool_use":
        if not show_tools:
            return None
        return f"  ↳ {obj.get('name', '?')}({obj.get('input', '')})"
    if kind == "result":
        return f"[result] {obj.get('result', '')}"
    return None


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
            with stream.path.open("r") as fh:
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
    tools: bool = typer.Option(False, "--tools", "-t", help="Include tool_use lines."),
    system: bool = typer.Option(False, "--system", "-s", help="Include system messages."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show tools + system."),
) -> None:
    """Tail orchestrator log files (grind-*, goal-*, ralph-*)."""
    target = resolve_repo(repo)
    logs_dir = target / "logs"
    if verbose:
        tools = True
        system = True
    _follow(logs_dir, raw=raw, show_tools=tools, show_system=system)
