"""Which issue owns the changed regions of one worktree path.

Path-level ownership is too coarse for a file two issues both touched, and a
worker's "this is not mine" declaration is made once, before it goes on to edit
the very path it disowned. This module answers the narrower question the absorb
step actually needs: *of the lines this path changes against HEAD, which
enclosing regions does the claimed issue's packet name?*

A region is a symbol from the CodeGraph index (code), or a heading span or
``<!-- BEGIN GENERATED: name -->`` block (Markdown). Anything that encloses no
changed line, and any changed line nothing encloses, is foreign — the failure
direction is refusing to absorb, never absorbing something unattributed.

The decision is whole-path by design: `own` re-adopts it, `foreign` honors the
declaration, and `mixed` is a planning gap for a human rather than a hunk-level
split nobody asked for.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable


#: The CodeGraph index, relative to the repository root. `.codegraph/` is
#: gitignored, so a fresh clone has none until `codegraph init` runs — which is
#: exactly the "unattributable" case rather than an error.
INDEX_RELATIVE = Path(".codegraph") / "codegraph.db"

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

#: A whole-file node is a legitimate region: module-level work in a file the
#: packet names as a concrete location is that issue's, and demanding an
#: enclosing symbol for every import and constant would refuse nearly every
#: real edit.
_FILE_KIND = "file"

_HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MD_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_BEGIN = re.compile(r"^\s*<!--\s*BEGIN GENERATED:\s*(.+?)\s*-->\s*$")
_MD_END = re.compile(r"^\s*<!--\s*END GENERATED:\s*(.+?)\s*-->\s*$")
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:[./][A-Za-z0-9_]+)*")


class Ownership(str, Enum):
    """What the claimed issue may do with a changed path it disowned."""

    OWN = "own"
    FOREIGN = "foreign"
    MIXED = "mixed"


@dataclass(frozen=True)
class Region:
    """One enclosing region of at least one changed line.

    `aliases` carries the other names the same region answers to — a symbol's
    qualified name, a file node's repository-relative path — so a packet that
    names the region either way still matches it.
    """

    name: str
    start: int
    end: int
    kind: str
    aliases: tuple[str, ...] = ()

    @property
    def attributable(self) -> bool:
        return bool(self.name)

    def label(self) -> str:
        span = f"{self.start}" if self.start == self.end else f"{self.start}-{self.end}"
        return f"{self.name or 'unattributed'} ({span})"


@dataclass(frozen=True)
class OwnershipDecision:
    """The verdict for one path, with the regions that produced it."""

    ownership: Ownership
    own: tuple[Region, ...] = ()
    foreign: tuple[Region, ...] = ()

    @property
    def regions(self) -> tuple[Region, ...]:
        return tuple(sorted((*self.own, *self.foreign), key=_region_order))


@dataclass(frozen=True)
class _Span:
    name: str
    kind: str
    start: int
    end: int
    aliases: tuple[str, ...] = field(default=())

    @property
    def width(self) -> int:
        return self.end - self.start


def _region_order(region: Region) -> tuple[int, int, str]:
    return (region.start, region.end, region.name)


# ---------------------------------------------------------------------------
# Changed lines
# ---------------------------------------------------------------------------


def changed_lines(repo: Path, path: str) -> frozenset[int]:
    """New-side line numbers `path` changes against HEAD.

    The diff is taken against HEAD rather than against the handoff state because
    HEAD is what a candidate commits: every uncommitted line in the path would
    land in the same commit, so every one of them has to be attributable before
    the path can be re-adopted. A path with no readable diff — deleted, binary,
    outside a repository — yields nothing, which resolves to foreign.
    """

    target = repo / path
    if not target.is_file():
        return frozenset()
    base = _diff_base(repo)
    if base is None:
        return frozenset()
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--unified=0", base, "--", path],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        return frozenset()
    lines: set[int] = set()
    for raw in diff.stdout.splitlines():
        match = _HUNK.match(raw)
        if match is None:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        if count == 0:
            # A pure deletion has no new-side line of its own; the seam it left
            # behind is what an enclosing region has to answer for.
            lines.add(max(start, 1))
            continue
        lines.update(range(start, start + count))
    if lines or _is_tracked(repo, path):
        return frozenset(lines)
    return _whole_file(target)


def _diff_base(repo: Path) -> str | None:
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if head.returncode == 0:
        return "HEAD"
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if inside.returncode != 0:
        return None
    return "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _is_tracked(repo: Path, path: str) -> bool:
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _whole_file(target: Path) -> frozenset[int]:
    try:
        payload = target.read_bytes()
    except OSError:
        return frozenset()
    if not payload:
        return frozenset()
    count = payload.count(b"\n") + (0 if payload.endswith(b"\n") else 1)
    return frozenset(range(1, max(count, 1) + 1))


# ---------------------------------------------------------------------------
# Region map
# ---------------------------------------------------------------------------


def region_map(
    repo: Path, path: str, lines: Iterable[int], *, index: Path | None = None
) -> tuple[Region, ...]:
    """The regions enclosing `lines` in `path`, each named once.

    Changed lines nothing encloses are reported too, as unattributed regions
    spanning the run they occupy, so a caller can name the line ranges it
    refused to absorb instead of reporting an empty region set.
    """

    numbers = sorted({int(line) for line in lines})
    if not numbers:
        return ()
    spans = (
        _markdown_spans(repo / path)
        if _is_markdown(path)
        else _code_spans(repo, path, index)
    )
    resolved: dict[tuple[str, int, int], Region] = {}
    orphans: list[int] = []
    for number in numbers:
        span = _smallest_enclosing(spans, number)
        if span is None:
            orphans.append(number)
            continue
        resolved.setdefault(
            (span.name, span.start, span.end),
            Region(span.name, span.start, span.end, span.kind, span.aliases),
        )
    regions = [*resolved.values(), *_orphan_regions(orphans)]
    return tuple(sorted(regions, key=_region_order))


def _is_markdown(path: str) -> bool:
    return Path(path).suffix.lower() in MARKDOWN_SUFFIXES


def _smallest_enclosing(spans: tuple[_Span, ...], line: int) -> _Span | None:
    enclosing = [span for span in spans if span.start <= line <= span.end]
    if not enclosing:
        return None
    # A generated block wins outright: it names its own generator, so a heading
    # written *inside* the markers is part of the generated output rather than
    # an owner of it. Otherwise narrowest wins, and a tie goes to the one that
    # starts latest — the inner of two regions that begin and end together.
    return min(
        enclosing,
        key=lambda span: (
            0 if span.kind == "marker" else 1,
            span.width,
            -span.start,
            span.name,
        ),
    )


def _orphan_regions(lines: list[int]) -> list[Region]:
    regions: list[Region] = []
    start: int | None = None
    previous: int | None = None
    for line in lines:
        if start is None:
            start = previous = line
            continue
        assert previous is not None
        if line == previous + 1:
            previous = line
            continue
        regions.append(Region("", start, previous, "unattributable"))
        start = previous = line
    if start is not None and previous is not None:
        regions.append(Region("", start, previous, "unattributable"))
    return regions


def _code_spans(repo: Path, path: str, index: Path | None) -> tuple[_Span, ...]:
    database = index if index is not None else repo / INDEX_RELATIVE
    if not database.is_file():
        return ()
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return ()
    try:
        rows = connection.execute(
            "SELECT name, qualified_name, kind, start_line, end_line FROM nodes "
            "WHERE file_path = ? AND start_line IS NOT NULL AND end_line IS NOT NULL",
            (path,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        connection.close()
    spans: list[_Span] = []
    for name, qualified, kind, start, end in rows:
        label = str(name or qualified or "")
        if not label:
            continue
        aliases = [str(qualified)] if qualified and qualified != name else []
        if str(kind) == _FILE_KIND:
            aliases.append(path)
        spans.append(
            _Span(
                label,
                str(kind or "symbol"),
                int(start),
                int(end),
                tuple(dict.fromkeys(alias for alias in aliases if alias != label)),
            )
        )
    return tuple(spans)


def _markdown_spans(file: Path) -> tuple[_Span, ...]:
    """Heading spans and generated-marker blocks, outermost first.

    A heading owns everything up to the next heading of the same or higher
    level, so the narrowest enclosing span of a line is the deepest heading
    over it. A generated block is narrower still and self-attributing: its
    marker names the generator that owns every line inside it.
    """

    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    lines = text.splitlines()
    spans: list[_Span] = []
    open_headings: list[tuple[int, str, int]] = []
    marker: tuple[str, int] | None = None
    fenced = False
    for number, line in enumerate(lines, start=1):
        if _MD_FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        begin = _MD_BEGIN.match(line)
        if begin is not None and marker is None:
            marker = (begin.group(1), number)
            continue
        end = _MD_END.match(line)
        if end is not None and marker is not None:
            spans.append(_Span(marker[0], "marker", marker[1], number))
            marker = None
            continue
        heading = _MD_HEADING.match(line)
        if heading is None:
            continue
        level = len(heading.group(1))
        while open_headings and open_headings[-1][0] >= level:
            _, name, start = open_headings.pop()
            spans.append(_Span(name, "heading", start, number - 1))
        open_headings.append((level, heading.group(2), number))
    for _, name, start in open_headings:
        spans.append(_Span(name, "heading", start, max(len(lines), start)))
    # An unterminated marker names no bounded block, so its lines stay
    # unattributed rather than swallowing the rest of the file.
    return tuple(spans)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def decide_ownership(
    regions: Iterable[Region], locations: str
) -> OwnershipDecision:
    """Split `regions` into ours and everyone else's against Concrete locations.

    Identifiers are matched against the section's backticked spans when it has
    any, because that is how a packet names a symbol or a file and prose around
    them would match far too much. Heading and marker names are phrases rather
    than identifiers, so they are matched as a word sequence anywhere in the
    section.
    """

    tokens = _location_tokens(locations)
    phrase = _normalise(locations)
    own: list[Region] = []
    foreign: list[Region] = []
    for region in regions:
        (own if _claims(region, tokens, phrase) else foreign).append(region)
    if not own and not foreign:
        return OwnershipDecision(Ownership.FOREIGN)
    if not foreign:
        return OwnershipDecision(Ownership.OWN, tuple(own))
    if not own:
        return OwnershipDecision(Ownership.FOREIGN, (), tuple(foreign))
    return OwnershipDecision(Ownership.MIXED, tuple(own), tuple(foreign))


def path_ownership(
    repo: Path, path: str, locations: str, *, index: Path | None = None
) -> OwnershipDecision:
    """Whether the claimed issue owns everything `path` changes against HEAD."""

    regions = region_map(repo, path, changed_lines(repo, path), index=index)
    return decide_ownership(regions, locations)


def describe(path: str, decision: OwnershipDecision) -> str:
    """One bounded line naming the path, its changed ranges, and the claimants."""

    ranges = ", ".join(
        f"{region.start}" if region.start == region.end else f"{region.start}-{region.end}"
        for region in decision.regions
    )
    ours = ", ".join(region.name for region in decision.own) or "none"
    theirs = ", ".join(region.label() for region in decision.foreign) or "none"
    return f"{path} lines {ranges or 'none'}; claimed here: {ours}; claimed elsewhere: {theirs}"


def _claims(region: Region, tokens: frozenset[str], phrase: str) -> bool:
    if not region.attributable:
        return False
    for candidate in (region.name, *region.aliases):
        if not candidate:
            continue
        if candidate in tokens:
            return True
        if region.kind in ("heading", "marker") and _phrase_match(candidate, phrase):
            return True
    return False


def _phrase_match(candidate: str, phrase: str) -> bool:
    normalised = _normalise(candidate)
    return bool(normalised) and f" {normalised} " in f" {phrase} "


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _location_tokens(locations: str) -> frozenset[str]:
    quoted = _BACKTICKED.findall(locations)
    corpus = "\n".join(quoted) if quoted else locations
    found: set[str] = set()
    for match in _TOKEN.finditer(corpus):
        text = match.group(0)
        found.add(text)
        segments = text.split("/")
        for index in range(len(segments)):
            found.add("/".join(segments[index:]))
        for segment in segments:
            found.add(segment)
            found.update(part for part in segment.split(".") if part)
    return frozenset(found)
