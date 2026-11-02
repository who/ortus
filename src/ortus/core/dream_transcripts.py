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

The Claude stream-json and Codex `exec --json` shapes are understood. A run
under any other backend is reported as skipped, with that backend named in
the reason, rather than parsed on a guess.

What a reader finally receives is labeled: a `[bead <id>]` header rebuilt
from bd on every run, then the window inside a `[grind-log ...]` label. Code
writes those labels; a line of tool output that only looks like one is
escaped, so a transcript cannot forge a memory or a past decision. The text
is scrubbed of credential values and this clone's protected terms, and the
prepared body is cached under `.ortus/dream/windows/` beside a sidecar that
says which log state produced it. The header is never cached, because a bead
edit must not invalidate a transcript that did not change.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ortus.core.agent_files import gitignore_match
from ortus.core.bd import BdError, ensure_protected_terms, scrub_protected_terms
from ortus.core.cost import _PREP, _RUN_START, _STAMPED
from ortus.core.spill import redact_secrets

#: Bumped when a prepared body changes for the same log, so caches keyed on
#: it are rebuilt rather than read back stale.
TRANSCRIPT_VERSION = 2

#: Any single string longer than this many UTF-8 bytes is cut.
DEFAULT_CUT_BYTES = 8192

#: How many bytes of a cut string survive in front of its marker.
CUT_KEEP_BYTES = 400

#: The `skipped` reason older caches recorded for any backend without an
#: adapter; new skips name the backend instead (see `skipped_reason`).
SKIPPED_UNKNOWN_SHAPE = "unknown-shape"

#: Backends whose stream this module parses. None is a run header that named
#: no backend, which only Claude-era logs wrote.
_CLAUDE_BACKENDS = (None, "claude")
_CODEX_BACKENDS = ("codex",)

#: Codex `exec --json` event types. `item.completed` carries every message,
#: command and its output; `turn.failed` and `error` carry failures. The rest
#: is lifecycle or usage bookkeeping: an `item.started` repeats the command
#: its `item.completed` reports, and an `item.updated` is a partial copy.
_CODEX_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)
_CODEX_DROPPED_TYPES = frozenset(
    {"thread.started", "turn.started", "turn.completed", "item.started", "item.updated"}
)

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


def _clean_codex_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """One Codex event without its item id, or None when it is pure noise."""

    if event.get("type") in _CODEX_DROPPED_TYPES:
        return None
    item = event.get("item")
    if isinstance(item, dict) and "id" in item:
        return {**event, "item": {k: v for k, v in item.items() if k != "id"}}
    return dict(event)


def skipped_reason(backend: str | None) -> str | None:
    """Why a window under `backend` is skipped, or None when it is parsed."""

    if backend in _CLAUDE_BACKENDS or backend in _CODEX_BACKENDS:
        return None
    return f"backend-{backend}"


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

    Plain text lines and JSON objects that are not events of the run's
    backend (Ortus' own records) are kept verbatim; only Claude or Codex
    events are cleaned, cut and re-serialized.
    """

    reason = skipped_reason(window.backend)
    if reason is not None:
        return PreparedWindow(window=window, body="", cuts=0, skipped=reason)
    if window.backend in _CODEX_BACKENDS:
        types, clean = _CODEX_TYPES, _clean_codex_event
    else:
        types, clean = _CLAUDE_TYPES, _clean_event
    kept: list[str] = []
    cuts = 0
    for line in window.lines:
        event = _as_object(line)
        if event is None or event.get("type") not in types:
            kept.append(line)
            continue
        cleaned = clean(event)
        if cleaned is None:
            continue
        cut, count = _cut(cleaned, cut_bytes)
        cuts += count
        kept.append(json.dumps(cut, ensure_ascii=False, separators=(",", ":")))
    return PreparedWindow(window=window, body="\n".join(kept), cuts=cuts)


# ---------------------------------------------------------------------------
# Labels, escaping and scrub
# ---------------------------------------------------------------------------

#: Every label kind Dream's code writes. A line of transcript text that opens
#: with one of these is escaped, whoever produced it.
LABEL_KINDS = (
    "bead",
    "grind-log",
    "reader-notes",
    "memory",
    "dream-decision",
    "need-answer",
)

_LABEL_LINE = re.compile(
    r"^(\s*)\[(/?)(" + "|".join(re.escape(kind) for kind in LABEL_KINDS) + r")\b",
    re.MULTILINE,
)

#: How much of a close reason and of each recent comment a header carries.
HEADER_CLOSE_REASON_CHARS = 160
HEADER_COMMENT_CHARS = 200
HEADER_COMMENTS = 2

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def window_label(window: BeadWindow) -> str:
    """The opening `[grind-log ...]` label naming where a window came from."""

    return (
        f"[grind-log bead={window.bead_id} run={window.run} "
        f"lines={window.start_line}-{window.end_line}]"
    )


def window_key(window: BeadWindow) -> str:
    """A window's cache name: `<stem>.<bead>.<a>-<b>`, safe on any filesystem."""

    run = _UNSAFE_NAME.sub("-", window.run)
    bead = _UNSAFE_NAME.sub("-", window.bead_id)
    return f"{run}.{bead}.{window.start_line}-{window.end_line}"


def escape_labels(text: str) -> str:
    """Backslash every line that opens like a Dream label.

    A JSON line starts with `{`, so a label-like string inside one is left
    alone; only text a reader could mistake for a code-made label changes.
    Escaping is idempotent: an escaped line no longer opens with `[`.
    """

    return _LABEL_LINE.sub(r"\1\\[\2\3", text)


def _one_line(value: Any, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    return text if limit is None else text[:limit]


def bead_header(issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    """The `[bead <id>]` block a reader sees above a bead's window."""

    bead_id = str(issue.get("id") or "unknown")
    labels = issue.get("labels") or []
    reason = _one_line(issue.get("close_reason"), HEADER_CLOSE_REASON_CHARS)
    recent = [
        _one_line(comment.get("text"), HEADER_COMMENT_CHARS)
        for comment in comments[-HEADER_COMMENTS:]
    ]
    lines = [
        f"[bead {bead_id}]",
        f"title:   {escape_labels(_one_line(issue.get('title')))}",
        f"labels:  {escape_labels(', '.join(str(label) for label in labels)) or 'none'}",
        f"status:  {_one_line(issue.get('status')) or 'unknown'}"
        f" · close_reason: {escape_labels(reason) or 'none'}",
        f"last comments (≤{HEADER_COMMENTS}, ≤{HEADER_COMMENT_CHARS} chars each):",
        *(f"  - {escape_labels(text)}" for text in recent),
        "[/bead]",
    ]
    return "\n".join(lines)


class _BeadReader(Protocol):
    def show(self, issue_id: str) -> dict[str, Any]: ...

    def comments(self, issue_id: str) -> list[dict[str, Any]]: ...


def load_bead_header(bd: _BeadReader, bead_id: str) -> str:
    """The header for `bead_id` as bd has it now; `status: unknown` when gone.

    A bead deleted since its run must not fail the whole Dream pass.
    """

    try:
        issue = bd.show(bead_id)
    except BdError:
        return bead_header({"id": bead_id}, [])
    try:
        comments = bd.comments(bead_id)
    except BdError:
        comments = []
    return bead_header(issue, comments)


def scrub(text: str, repo: Path) -> str:
    """`text` with credential values and this clone's protected terms removed."""

    return scrub_protected_terms(redact_secrets(text), ensure_protected_terms(repo))


def render_window(prepared: PreparedWindow, header: str) -> str:
    """The labeled text one reader receives for one bead window."""

    return "\n".join(
        (
            header,
            window_label(prepared.window),
            escape_labels(prepared.body),
            "[/grind-log]",
        )
    )


# ---------------------------------------------------------------------------
# Window cache
# ---------------------------------------------------------------------------

#: Where prepared windows are cached, relative to the repository.
CACHE_DIR = Path(".ortus") / "dream"
WINDOWS_DIR = CACHE_DIR / "windows"


class DreamCacheNotIgnored(RuntimeError):
    """The Dream cache directory is not git-ignored, so nothing is written."""


def ensure_cache_ignored(repo: Path) -> None:
    """Refuse a cache write that would show up as an untracked worktree file."""

    if gitignore_match(repo, CACHE_DIR.as_posix()) is None:
        raise DreamCacheNotIgnored(
            f"{CACHE_DIR.as_posix()}/ is not ignored in {repo}; add it to "
            ".gitignore (`ortus init --force` does) before running Dream"
        )


def _sidecar(window: BeadWindow, cut_bytes: int) -> dict[str, Any] | None:
    """The fields a cached window must match, or None when the log is gone."""

    try:
        stat = window.log_path.stat()
    except OSError:
        return None
    return {
        "log_path": str(window.log_path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "start_line": window.start_line,
        "end_line": window.end_line,
        "transcript_version": TRANSCRIPT_VERSION,
        "cut_bytes": cut_bytes,
    }


def _cached(
    window: BeadWindow, text_path: Path, meta_path: Path, expected: dict[str, Any]
) -> PreparedWindow | None:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = text_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    if any(meta.get(key) != value for key, value in expected.items()):
        return None
    cuts = meta.get("cuts")
    skipped = meta.get("skipped")
    if not isinstance(cuts, int) or not (skipped is None or isinstance(skipped, str)):
        return None
    return PreparedWindow(window=window, body=body, cuts=cuts, skipped=skipped)


def load_or_prepare(
    window: BeadWindow, *, repo: Path, cut_bytes: int = DEFAULT_CUT_BYTES
) -> PreparedWindow:
    """The window prepared, escaped and scrubbed, read from cache when current.

    The cache is reused only when its sidecar matches the log's size and
    mtime, the window's line range, the transcript version and `cut_bytes`;
    anything else rewrites it. A skipped window is cached as skipped with an
    empty body, so it is counted rather than parsed again.
    """

    directory = repo / WINDOWS_DIR
    key = window_key(window)
    text_path = directory / f"{key}.txt"
    meta_path = directory / f"{key}.json"
    expected = _sidecar(window, cut_bytes)
    if expected is not None:
        cached = _cached(window, text_path, meta_path, expected)
        if cached is not None:
            return cached

    prepared = prepare_window(window, cut_bytes=cut_bytes)
    body = scrub(escape_labels(prepared.body), repo) if prepared.body else ""
    result = PreparedWindow(
        window=window, body=body, cuts=prepared.cuts, skipped=prepared.skipped
    )
    if expected is None:
        return result
    ensure_cache_ignored(repo)
    directory.mkdir(parents=True, exist_ok=True)
    text_path.write_text(body, encoding="utf-8")
    meta = {**expected, "cuts": result.cuts, "skipped": result.skipped}
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return result
