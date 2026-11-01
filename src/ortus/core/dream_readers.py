"""Dream's reader stage: one read-only agent per bead window, notes cached.

Each selected window is rendered with its bead header, written to an input
file under `.ortus/dream/inputs/`, and handed to the configured backend in
read-only mode. The window travels in a file rather than in argv because
Linux caps one argv string at 128 KiB and a prepared window can be larger;
the prompt only names the file.

A window over `reader_chunk_tokens` is split at line boundaries into chunks
read in sequence. Each chunk carries its own `[grind-log ...]` label with
the true line sub-range, and every chunk after the first also carries the
notes the previous chunk produced, so the last chunk's notes cover the whole
window and are the only ones cached.

The reader answers with `ORTUS_DREAM_NOTE` lines and one `ORTUS_DREAM_WINDOW`
line. A note with the wrong shape is dropped and counted; a missing or
duplicated summary line, a nonzero exit or a timeout fails the window, and a
failed window writes no notes file so the next run reads it again.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from ortus.core import output
from ortus.core.dream_transcripts import (
    CACHE_DIR,
    BeadWindow,
    PreparedWindow,
    escape_labels,
    prepare_window,
    render_window,
    scrub,
    window_key,
    window_label,
)
from ortus.core.profiles import AgentProfile
from ortus.core.verdict import assistant_text

#: Bytes per token in every Dream estimate; the command module uses the same.
BYTES_PER_TOKEN = 3.5

#: One reader call's wall clock.
READER_TIMEOUT_S = 900

INPUTS_DIR = CACHE_DIR / "inputs"
RUNS_DIR = CACHE_DIR / "runs"

NOTE_PREFIX = "ORTUS_DREAM_NOTE:"
WINDOW_PREFIX = "ORTUS_DREAM_WINDOW:"

KIND_HINTS = frozenset({"memory", "followup", "none"})

#: Context windows by model-name prefix; the longest matching prefix wins.
CONTEXT_WINDOW_TOKENS = {
    "claude-haiku-5-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-sonnet": 1_000_000,
    "claude-opus": 1_000_000,
}


class DreamReaderError(RuntimeError):
    """The reader stage cannot run at all; the message names why."""


class _Runner(Protocol):
    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        profile: AgentProfile | None = None,
        timeout: float | None = None,
        readonly: bool = False,
    ) -> int: ...

    def preflight_readonly(self, repo: Path, *, timeout: float = 60.0) -> None: ...


# ---------------------------------------------------------------------------
# Context guard
# ---------------------------------------------------------------------------


def context_window(model: str | None) -> int | None:
    """The model's context size by longest known prefix, or None if unknown."""

    if not model:
        return None
    matches = [prefix for prefix in CONTEXT_WINDOW_TOKENS if model.startswith(prefix)]
    if not matches:
        return None
    return CONTEXT_WINDOW_TOKENS[max(matches, key=len)]


def check_context(model: str | None, chunk_tokens: int) -> str | None:
    """Refuse a chunk size over half the model's context; warn when unknown.

    Returns a warning for an unknown model, None when the size fits, and
    raises `DreamReaderError` naming both numbers when it does not.
    """

    size = context_window(model)
    if size is None:
        return (
            f"reader model {model or '(backend default)'} has no known context "
            f"window; proceeding with reader_chunk_tokens={chunk_tokens}"
        )
    if chunk_tokens > 0.5 * size:
        raise DreamReaderError(
            f"reader_chunk_tokens={chunk_tokens} exceeds half of the "
            f"{size}-token context window of {model}"
        )
    return None


# ---------------------------------------------------------------------------
# Chunking and input files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One reader call's slice of a window: its label, body and line range."""

    index: int
    start_line: int
    end_line: int
    body: str


def _tokens(size: int) -> float:
    return size / BYTES_PER_TOKEN


def numbered_lines(
    window: BeadWindow, *, cut_bytes: int
) -> list[tuple[int, str]]:
    """Each source line's prepared text with its log line number.

    The cached body drops lines and so loses its numbering; preparing line
    by line keeps it, which is what lets a chunk name its true sub-range.
    """

    kept: list[tuple[int, str]] = []
    for offset, line in enumerate(window.lines):
        number = window.start_line + offset
        single = replace(window, lines=(line,), start_line=number, end_line=number)
        body = prepare_window(single, cut_bytes=cut_bytes).body
        if body:
            kept.append((number, body))
    return kept


def split_chunks(
    lines: list[tuple[int, str]], *, budget_bytes: int
) -> list[Chunk]:
    """Pack numbered lines into chunks whose bodies stay under `budget_bytes`.

    A single line larger than the budget is its own chunk.
    """

    chunks: list[Chunk] = []
    current: list[tuple[int, str]] = []
    size = 0
    for number, text in lines:
        cost = len(text.encode("utf-8")) + 1
        if current and size + cost > budget_bytes:
            chunks.append(_chunk(len(chunks) + 1, current))
            current, size = [], 0
        current.append((number, text))
        size += cost
    if current:
        chunks.append(_chunk(len(chunks) + 1, current))
    return chunks


def _chunk(index: int, lines: list[tuple[int, str]]) -> Chunk:
    return Chunk(
        index=index,
        start_line=lines[0][0],
        end_line=lines[-1][0],
        body="\n".join(text for _, text in lines),
    )


def window_chunks(
    prepared: PreparedWindow,
    header: str,
    *,
    chunk_tokens: int,
    cut_bytes: int,
    repo: Path,
) -> list[tuple[BeadWindow, str]]:
    """The labeled window, or its chunks, as (sub-window, body) pairs.

    A window that fits is one chunk carrying the cached body unchanged.
    """

    window = prepared.window
    rendered = render_window(prepared, header)
    if _tokens(len(rendered.encode("utf-8"))) <= chunk_tokens:
        return [(window, prepared.body)]
    overhead = len(header.encode("utf-8")) + len(window_label(window)) + 64
    budget = max(1, int(chunk_tokens * BYTES_PER_TOKEN) - overhead)
    chunks = split_chunks(numbered_lines(window, cut_bytes=cut_bytes), budget_bytes=budget)
    return [
        (
            replace(window, start_line=chunk.start_line, end_line=chunk.end_line),
            scrub(escape_labels(chunk.body), repo),
        )
        for chunk in chunks
    ]


def note_lines(notes: Iterable[dict[str, Any]]) -> str:
    return "\n".join(
        f"{NOTE_PREFIX} {json.dumps(note, ensure_ascii=False)}" for note in notes
    )


def compose_input(
    sub_window: BeadWindow,
    body: str,
    header: str,
    *,
    chunk: int,
    previous_notes: list[dict[str, Any]] | None,
) -> str:
    """The text of one chunk's input file: header, carried notes, window."""

    parts = [header]
    if chunk > 1:
        parts.append(
            f"[reader-notes bead={sub_window.bead_id} run={sub_window.run} "
            f"chunk={chunk - 1}]"
        )
        carried = note_lines(previous_notes or [])
        if carried:
            parts.append(carried)
        parts.append("[/reader-notes]")
    parts.extend((window_label(sub_window), escape_labels(body), "[/grind-log]"))
    return "\n".join(parts) + "\n"


def reader_prompt(brief: str, input_path: Path) -> str:
    """The argv prompt: the brief, then the one line naming the input file."""

    return (
        f"{brief.rstrip()}\n\nYour input is the file {input_path.resolve()}. "
        "Read all of it, in successive offset/limit reads until the end, "
        "before you answer.\n"
    )


# ---------------------------------------------------------------------------
# Parsing a reader's answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedAnswer:
    notes: tuple[dict[str, Any], ...]
    window: dict[str, Any] | None
    invalid_notes: int
    error: str | None
    usage: dict[str, Any] | None
    cost_usd: float | None


def valid_note(note: Any) -> bool:
    """Whether a decoded NOTE has the shape the brief asks for."""

    if not isinstance(note, dict):
        return False
    issue = note.get("issue")
    if not isinstance(issue, str) or not issue.strip():
        return False
    evidence = note.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    for item in evidence:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("label"), str) or not isinstance(item.get("quote"), str):
            return False
    if note.get("kind_hint") not in KIND_HINTS:
        return False
    return isinstance(note.get("memory_would_help"), bool)


def _events(log_path: Path) -> Iterable[dict[str, Any]]:
    try:
        handle = log_path.open("rb")
    except OSError:
        return
    with handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                yield event


def parse_answer(log_path: Path) -> ParsedAnswer:
    """Notes, the summary line and usage from one reader call's log."""

    notes: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    invalid = 0
    result: dict[str, Any] | None = None
    for event in _events(log_path):
        if event.get("type") == "result":
            result = event
        for text in assistant_text(event):
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith(NOTE_PREFIX):
                    try:
                        note = json.loads(stripped[len(NOTE_PREFIX):].strip())
                    except json.JSONDecodeError:
                        invalid += 1
                        continue
                    if valid_note(note):
                        notes.append(note)
                    else:
                        invalid += 1
                elif stripped.startswith(WINDOW_PREFIX):
                    try:
                        summary = json.loads(stripped[len(WINDOW_PREFIX):].strip())
                    except json.JSONDecodeError:
                        summary = None
                    windows.append(summary if isinstance(summary, dict) else {})
    error = None
    if not windows:
        error = "no ORTUS_DREAM_WINDOW line"
    elif len(windows) > 1:
        error = f"{len(windows)} ORTUS_DREAM_WINDOW lines"
    usage = result.get("usage") if result is not None else None
    cost = result.get("total_cost_usd") if result is not None else None
    return ParsedAnswer(
        notes=tuple(notes),
        window=windows[0] if len(windows) == 1 else None,
        invalid_notes=invalid,
        error=error,
        usage=usage if isinstance(usage, dict) else None,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


# ---------------------------------------------------------------------------
# Running readers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReaderResult:
    """One window's reader outcome; `notes_file` is None when it failed."""

    window_key: str
    notes: tuple[dict[str, Any], ...]
    invalid_notes: int
    chunks: int
    error: str | None
    notes_file: Path | None


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def run_reader(
    prepared: PreparedWindow,
    *,
    runner: _Runner,
    repo: Path,
    header: str,
    brief: str,
    key: str,
    backend: str,
    profile: AgentProfile,
    chunk_tokens: int,
    cut_bytes: int,
    run_dir: Path,
    notes_file: Path,
    log: Callable[[str], None],
) -> ReaderResult:
    """Read one window, chunk by chunk, and cache its notes when it succeeds."""

    window = prepared.window
    wkey = window_key(window)
    chunks = window_chunks(
        prepared, header, chunk_tokens=chunk_tokens, cut_bytes=cut_bytes, repo=repo
    )
    notes: list[dict[str, Any]] = []
    invalid = 0
    answer: ParsedAnswer | None = None
    usage_total: dict[str, Any] | None = None
    cost_total: float | None = None
    for index, (sub_window, body) in enumerate(chunks, start=1):
        input_path = repo / INPUTS_DIR / f"{wkey}.{key}.chunk{index}.md"
        _write_atomic(
            input_path,
            compose_input(
                sub_window, body, header, chunk=index, previous_notes=notes
            ),
        )
        call_log = run_dir / f"{wkey}.chunk{index}.jsonl"
        call_log.parent.mkdir(parents=True, exist_ok=True)
        log(f"reader {wkey} chunk {index}/{len(chunks)}: {window_label(sub_window)}")
        try:
            code = runner.run(
                reader_prompt(brief, input_path),
                repo=repo,
                log_path=call_log,
                profile=profile,
                timeout=READER_TIMEOUT_S,
                readonly=True,
            )
        except subprocess.TimeoutExpired:
            return _failed(wkey, len(chunks), f"chunk {index} timed out", log)
        if code != 0:
            return _failed(wkey, len(chunks), f"chunk {index} exited {code}", log)
        answer = parse_answer(call_log)
        if answer.error is not None:
            return _failed(wkey, len(chunks), f"chunk {index}: {answer.error}", log)
        notes = list(answer.notes)
        invalid += answer.invalid_notes
        if answer.usage is not None:
            usage_total = _add_usage(usage_total, answer.usage)
        if answer.cost_usd is not None:
            cost_total = (cost_total or 0.0) + answer.cost_usd
    assert answer is not None
    record = {
        "window_key": wkey,
        "label": window_label(window),
        "bead": window.bead_id,
        "run": window.run,
        "lines": f"{window.start_line}-{window.end_line}",
        "reader_key": key,
        "backend": backend,
        "model": profile.model,
        "effort": profile.reasoning_effort,
        "notes": notes,
        "window": answer.window,
        "invalid_notes": invalid,
        "chunks": len(chunks),
        "usage": usage_total,
        "cost_usd": cost_total,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    try:
        _write_atomic(
            notes_file, json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as exc:
        log(f"reader {wkey}: notes write failed: {exc}")
        raise
    log(f"reader {wkey}: {len(notes)} note(s), {invalid} invalid")
    return ReaderResult(
        window_key=wkey,
        notes=tuple(notes),
        invalid_notes=invalid,
        chunks=len(chunks),
        error=None,
        notes_file=notes_file,
    )


def _add_usage(total: dict[str, Any] | None, usage: dict[str, Any]) -> dict[str, Any]:
    """Sum numeric usage fields across chunks; the rest keep the last value."""

    merged = dict(total or {})
    for name, value in usage.items():
        previous = merged.get(name)
        if isinstance(value, (int, float)) and isinstance(previous, (int, float)):
            merged[name] = previous + value
        else:
            merged[name] = value
    return merged


def _failed(
    wkey: str, chunks: int, reason: str, log: Callable[[str], None]
) -> ReaderResult:
    log(f"reader {wkey} failed: {reason}; no notes cached, the next run retries it")
    return ReaderResult(
        window_key=wkey,
        notes=(),
        invalid_notes=0,
        chunks=chunks,
        error=reason,
        notes_file=None,
    )


def run_log_writer(repo: Path, stamp: str) -> Callable[[str], None]:
    """`logs/dream-<stamp>.log` writer that also reports each line as progress."""

    path = repo / "logs" / f"dream-{stamp}.log"

    def _write(message: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now}] {message}\n")
        output.progress("dream", message)

    return _write


def run_readers(
    candidates: Iterable[PreparedWindow],
    *,
    runner: _Runner,
    repo: Path,
    header_for: Callable[[str], str],
    brief: str,
    key: str,
    backend: str,
    profile: AgentProfile,
    chunk_tokens: int,
    cut_bytes: int,
    notes_path_for: Callable[[PreparedWindow], Path],
    stamp: str | None = None,
) -> list[ReaderResult]:
    """Run one reader per candidate window, sequentially, caching the notes.

    Raises `DreamReaderError` when the chunk size exceeds the model's context
    guard or the read-only preflight fails; one window's failure does not stop
    the others.
    """

    items = list(candidates)
    stamp = stamp or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = run_log_writer(repo, stamp)
    warning = check_context(profile.model, chunk_tokens)
    if warning is not None:
        output.warn(warning)
        log(f"warning: {warning}")
    if not items:
        log("readers: no window needs a reader")
        return []
    try:
        runner.preflight_readonly(repo)
    except RuntimeError as exc:
        raise DreamReaderError(f"read-only preflight failed: {exc}") from exc
    run_dir = repo / RUNS_DIR / stamp
    log(
        f"readers: {len(items)} window(s) with {profile.display_name} "
        f"(each call up to {READER_TIMEOUT_S // 60} min)"
    )
    results = []
    for position, prepared in enumerate(items, start=1):
        log(f"reader {position}/{len(items)}: {window_label(prepared.window)}")
        results.append(
            run_reader(
                prepared,
                runner=runner,
                repo=repo,
                header=header_for(prepared.window.bead_id),
                brief=brief,
                key=key,
                backend=backend,
                profile=profile,
                chunk_tokens=chunk_tokens,
                cut_bytes=cut_bytes,
                run_dir=run_dir,
                notes_file=notes_path_for(prepared),
                log=log,
            )
        )
    failed = sum(1 for result in results if result.error is not None)
    log(f"readers done ({len(results) - failed} cached, {failed} failed)")
    return results
