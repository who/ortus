"""Grind logs cut into one window per bead, cleaned of transport noise.

A grind log is one file per run covering several beads, and it is not pure
JSONL: Ortus' own stamped text lines, the agent's stream-json events and
Ortus' JSON records are interleaved. Dream reads it a bead at a time, so the
first job is to find where one bead's worker begins and the next one's does.
The only marker that names the bead before its worker runs is the `iter
prep` line, and the grammar for it is imported from `cost` rather than
restated: a marker change has to break the cost rollup and Dream together.

Preparing a window makes no judgment about what matters. Cleanup removes
only what the transport repeats or stamps on every line — envelope ids, rate
limit and progress ticks, and the `tool_use_result` copy of a tool result the
same line already carries. Every message, tool call, tool result, error flag,
thinking block, system event and result record stays. The one shortening is
a cut of any single string past a byte threshold, which leaves its head and a
marker naming how much went, so a huge file read or test dump cannot crowd
out the rest of the window.

Only the Claude stream-json shape is understood. A run under another backend
is reported as skipped rather than parsed on a guess.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ortus.core.cost import _PREP, _RUN_START, _STAMPED

#: Bumped when a prepared body changes for the same log, so caches keyed on
#: it are rebuilt rather than read back stale.
TRANSCRIPT_VERSION = 1

#: Any single string longer than this many UTF-8 bytes is cut.
DEFAULT_CUT_BYTES = 8192

#: How many bytes of a cut string survive in front of its marker.
CUT_KEEP_BYTES = 400

#: The `skipped` reason for a run whose stream shape has no adapter yet.
SKIPPED_UNKNOWN_SHAPE = "unknown-shape"

#: Backends whose stream this module parses. None is a run header that named
#: no backend, which only Claude-era logs wrote.
_CLAUDE_BACKENDS = (None, "claude")

#: Claude stream-json event types; any other JSON object is kept verbatim.
_CLAUDE_TYPES = frozenset(
    {
        "assistant",
        "user",
        "system",
        "result",
        "rate_limit_event",
        "tool_progress",
        "stream_event",
    }
)
_DROPPED_TYPES = frozenset({"rate_limit_event", "tool_progress"})
_ENVELOPE_KEYS = ("session_id", "uuid", "parent_uuid", "parent_tool_use_id")


@dataclass(frozen=True)
class BeadWindow:
    """One bead's slice of a grind log; line numbers are 1-based, inclusive."""

    log_path: Path
    run: str
    bead_id: str
    start_line: int
    end_line: int
    backend: str | None
    lines: tuple[str, ...]


@dataclass(frozen=True)
class PreparedWindow:
    """A window's cleaned body, how many strings were cut, or why it was not."""

    window: BeadWindow
    body: str
    cuts: int
    skipped: str | None = None


def _stamped_body(line: str) -> str | None:
    match = _STAMPED.match(line)
    return None if match is None else match.group("body")


def split_windows(log_path: Path) -> tuple[BeadWindow, ...]:
    """Every bead window in one grind log, in log order.

    A window opens on a stamped `iter prep` line and runs to the line before
    the next one, or to the end of the file. What precedes the first marker
    is the run header: it names the backend and belongs to no window.
    """

    text = log_path.read_bytes().decode("utf-8", errors="replace")
    lines = text.splitlines()
    backend: str | None = None
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        body = _stamped_body(line)
        if body is None:
            continue
        prep = _PREP.match(body)
        if prep is not None:
            starts.append((index, prep.group("issue")))
            continue
        if not starts and backend is None:
            run_start = _RUN_START.match(body)
            if run_start is not None:
                backend = run_start.group("backend")

    windows: list[BeadWindow] = []
    for position, (start, bead_id) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        windows.append(
            BeadWindow(
                log_path=log_path,
                run=log_path.stem,
                bead_id=bead_id,
                start_line=start + 1,
                end_line=end,
                backend=backend,
                lines=tuple(lines[start:end]),
            )
        )
    return tuple(windows)


def _as_object(line: str) -> dict[str, Any] | None:
    """The line as a JSON object, or None; never decided by substring."""

    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """One Claude event without its envelope, or None when it is pure noise."""

    kind = event.get("type")
    if kind in _DROPPED_TYPES:
        return None
    cleaned = {key: value for key, value in event.items() if key not in _ENVELOPE_KEYS}
    if kind == "user":
        cleaned.pop("tool_use_result", None)
    return cleaned


def _cut_string(value: str, cut_bytes: int) -> tuple[str, int]:
    encoded = value.encode("utf-8")
    if len(encoded) <= cut_bytes:
        return value, 0
    head = encoded[:CUT_KEEP_BYTES].decode("utf-8", errors="ignore")
    return f"{head} [cut {math.ceil(len(encoded) / 1024)} KB]", 1


def _cut(value: Any, cut_bytes: int) -> tuple[Any, int]:
    """`value` with every oversized string cut, and how many were."""

    if isinstance(value, str):
        return _cut_string(value, cut_bytes)
    if isinstance(value, dict):
        total = 0
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key], count = _cut(item, cut_bytes)
            total += count
        return out, total
    if isinstance(value, list):
        total = 0
        items: list[Any] = []
        for item in value:
            cut_item, count = _cut(item, cut_bytes)
            items.append(cut_item)
            total += count
        return items, total
    return value, 0


def prepare_window(
    window: BeadWindow, *, cut_bytes: int = DEFAULT_CUT_BYTES
) -> PreparedWindow:
    """The window's body with transport noise removed and huge strings cut.

    Plain text lines and JSON objects that are not Claude events (Ortus'
    own records) are kept verbatim; only Claude events are cleaned, cut and
    re-serialized.
    """

    if window.backend not in _CLAUDE_BACKENDS:
        return PreparedWindow(
            window=window, body="", cuts=0, skipped=SKIPPED_UNKNOWN_SHAPE
        )
    kept: list[str] = []
    cuts = 0
    for line in window.lines:
        event = _as_object(line)
        if event is None or event.get("type") not in _CLAUDE_TYPES:
            kept.append(line)
            continue
        cleaned = _clean_event(event)
        if cleaned is None:
            continue
        cut, count = _cut(cleaned, cut_bytes)
        cuts += count
        kept.append(json.dumps(cut, ensure_ascii=False, separators=(",", ":")))
    return PreparedWindow(window=window, body="\n".join(kept), cuts=cuts)
