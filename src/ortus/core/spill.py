"""Large output goes to a file; the comment or prompt gets a reference.

Ortus' durable surfaces are budgeted in characters: a bead comment is read
by a person and re-read by the next worker, and a prompt segment competes
with every other segment for one context window. The old answer to a body
that would not fit was a middle ellipsis — the head and the tail survived
and everything between them was destroyed at the moment of summarizing,
which is the one loss a later reader cannot undo.

This module keeps the body instead. Output over the inline threshold is
written whole to a file under a spill directory the same seat can read, and
what enters the comment or the prompt is the path, the byte size, and a
short tail. A reader who needs the verdict reads the tail; a reader who
needs the middle opens the file. Only a spill that could not be written at
all falls back to bounded excerpts, and that fallback names itself rather
than passing for a complete body.

Redaction applies to what is rendered, never to what is stored. A comment
and a prompt outlive the run and travel; the spilled file is a local
artifact under a directory `logs/` already ignores, and an operator reading
it back is owed the bytes the command actually produced. Authored text is a
third case: a work spec is the operator's own prose, so callers that render
packet fields ask for the size bound without the masking that would corrupt
a criterion naming a credential variable.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Output at or under this many characters is small enough to read in place.
DEFAULT_INLINE_LIMIT = 4_000
#: How much of an oversized body's end travels with the reference.
DEFAULT_TAIL_CHARS = 1_500
#: Spilled bodies land here, beside the grind logs when a repository is known.
SPILL_DIRNAME = "ortus-output"
#: Reached only when the body could not be written anywhere at all.
TRUNCATION_MARKER = "\n[... output truncated ...]\n"

#: The value half of a credential assignment, in the shapes command output
#: actually prints it. Shared with the verifier report so one pattern governs
#: every rendered surface.
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)([^\r\n]+)"
)

_NAME_JUNK = re.compile(r"[^A-Za-z0-9._-]+")
#: utf-8's widest encoding, used to over-read an end before decoding it.
_MAX_BYTES_PER_CHAR = 4


def redact_secrets(value: str) -> str:
    """Mask the value half of every credential assignment in `value`."""
    return SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)


def default_spill_dir(repo: Path | None = None) -> Path:
    """`logs/` beside the grind logs, or the system temp dir with no repo."""
    if repo is not None:
        return Path(repo) / "logs" / SPILL_DIRNAME
    return Path(tempfile.gettempdir()) / SPILL_DIRNAME


@dataclass(frozen=True)
class SpilledOutput:
    """One captured body: small enough to inline, or spilled to a file.

    ``inline`` carries the whole body when it fit. Otherwise ``path`` names
    the file holding it and ``tail`` is the end that travels with the
    reference; ``head`` is held back for the one case where no file could be
    written, so a failed spill still shows both ends rather than nothing.
    """

    inline: str = ""
    head: str = ""
    tail: str = ""
    path: Path | None = None
    size_bytes: int = 0
    oversized: bool = False
    error: str = ""
    note: str = ""
    redact: bool = True

    @property
    def spilled(self) -> bool:
        return self.path is not None

    def _clean(self, value: str) -> str:
        return redact_secrets(value) if self.redact else value

    def render(self) -> str:
        """Comment- and prompt-safe text: the body, or a reference to it."""
        if not self.oversized:
            return self._clean(self.inline)
        note = f", {self.note}" if self.note else ""
        if self.path is not None:
            header = (
                f"[full output: {self.path} — {self.size_bytes} bytes{note}; "
                f"last {len(self.tail)} characters follow]"
            )
            return header + "\n" + self._clean(self.tail)
        header = (
            f"[full output not retained: {self.error or 'no spill directory'}"
            f"{note}; {self.size_bytes} bytes produced]"
        )
        return (
            header
            + "\n"
            + self._clean(self.head)
            + TRUNCATION_MARKER
            + self._clean(self.tail)
        )


def _slug(name: str) -> str:
    """A file-name stem from a criterion id, field name, or command label."""
    cleaned = _NAME_JUNK.sub("-", str(name)).strip("-")
    return cleaned[:60] or "output"


def _note_for(text: str) -> str:
    return "decoded with replacement characters" if "�" in text else ""


def _ends(text: str, limit: int, tail_chars: int | None) -> tuple[str, str]:
    """The two excerpts a reference may show, together bounded by `limit`."""
    wanted = DEFAULT_TAIL_CHARS if tail_chars is None else tail_chars
    tail_size = max(1, min(wanted, limit))
    head_size = max(0, limit - tail_size)
    return text[:head_size], text[-tail_size:]


def _write(spill_dir: Path | None, name: str, body: bytes | Path) -> tuple[Path | None, str]:
    """Write or copy `body` into `spill_dir` under a unique name.

    Returns the path, or an empty path and the reason it could not be
    written. An unwritable spill directory degrades the reference; it never
    raises into a check run or a prompt composition, because a body that
    cannot be filed is still a body the caller must say something about.
    """
    target = default_spill_dir() if spill_dir is None else Path(spill_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
        handle, raw = tempfile.mkstemp(
            prefix=f"{_slug(name)}-", suffix=".log", dir=str(target)
        )
        path = Path(raw)
        if isinstance(body, Path):
            os.close(handle)
            shutil.copyfile(body, path)
        else:
            with os.fdopen(handle, "wb") as sink:
                sink.write(body)
    except OSError as exc:
        return None, f"could not write under {target}: {exc}"
    return path, ""


def spill_large_output(
    data: str | bytes,
    *,
    spill_dir: Path | None = None,
    limit: int = DEFAULT_INLINE_LIMIT,
    tail_chars: int | None = None,
    name: str = "output",
    redact: bool = True,
) -> SpilledOutput:
    """Keep `data` whole: inlined when small, on disk plus a tail when not."""
    if isinstance(data, bytes):
        blob = data
        text = blob.decode("utf-8", errors="replace")
    else:
        text = data
        blob = text.encode("utf-8", errors="replace")
    note = _note_for(text)
    if len(text) <= limit:
        return SpilledOutput(
            inline=text, size_bytes=len(blob), note=note, redact=redact
        )
    head, tail = _ends(text, limit, tail_chars)
    path, error = _write(spill_dir, name, blob)
    return SpilledOutput(
        head=head,
        tail=tail,
        path=path,
        size_bytes=len(blob),
        oversized=True,
        error=error,
        note=note,
        redact=redact,
    )


def spill_output_file(
    source: Path,
    *,
    spill_dir: Path | None = None,
    limit: int = DEFAULT_INLINE_LIMIT,
    tail_chars: int | None = None,
    name: str | None = None,
    redact: bool = True,
) -> SpilledOutput:
    """The same contract for output a command already captured to a file.

    An oversized capture is never read into memory whole: the file is copied
    to the spill directory and only its two ends are decoded for the
    reference, so a check that printed a gigabyte costs the same as one that
    printed a megabyte. A capture that cannot be read at all renders as
    nothing, exactly as a missing capture did before.
    """
    try:
        size = source.stat().st_size
    except OSError:
        return SpilledOutput(redact=redact)
    label = source.stem if name is None else name
    if size <= limit:
        # utf-8 never decodes to more characters than it has bytes, so a
        # capture this small is inlinable without a second measurement.
        try:
            text = source.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return SpilledOutput(redact=redact)
        return SpilledOutput(
            inline=text, size_bytes=size, note=_note_for(text), redact=redact
        )
    head, tail, note = _file_ends(source, size, limit, tail_chars)
    path, error = _write(spill_dir, label, source)
    return SpilledOutput(
        head=head,
        tail=tail,
        path=path,
        size_bytes=size,
        oversized=True,
        error=error,
        note=note,
        redact=redact,
    )


def _file_ends(
    source: Path, size: int, limit: int, tail_chars: int | None
) -> tuple[str, str, str]:
    """Decode only the two ends of a capture too large to hold in memory.

    Each end is over-read by utf-8's widest encoding and then trimmed in
    characters, so the excerpts obey the same character bound as an in-memory
    body. Seeking to the tail can land inside a multi-byte sequence; that
    produces a replacement character the trim then drops, and the note says
    so when any survives.
    """
    probe_head, probe_tail = _ends("x" * (limit + 1), limit, tail_chars)
    head_chars, tail_size = len(probe_head), len(probe_tail)
    try:
        with source.open("rb") as handle:
            raw_head = handle.read(head_chars * _MAX_BYTES_PER_CHAR)
            handle.seek(max(0, size - tail_size * _MAX_BYTES_PER_CHAR))
            raw_tail = handle.read()
    except OSError as exc:
        return "", "", f"ends unreadable: {exc}"
    head = raw_head.decode("utf-8", errors="replace")[:head_chars]
    tail = raw_tail.decode("utf-8", errors="replace")[-tail_size:]
    return head, tail, _note_for(head + tail)
