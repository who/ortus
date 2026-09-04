"""Ortus-managed blocks inside a repo's `AGENTS.md` and `CLAUDE.md`.

Both files belong to the consumer repo, not to Ortus. A whole-file template
can only be written by destroying whatever the repo already taught its agents,
so Ortus owns a marked region instead:

    <!-- BEGIN ortus block=agents schema=1 generated-by=ortus@0.1.0 -->
    ...rendered body...
    <!-- END ortus block=agents -->

Everything outside the markers is host prose and is copied through untouched.
The BEGIN marker carries the block name, the block's schema integer, and the
Ortus that wrote it, which is what lets `ortus init` decide between creating,
appending, replacing, and standing down, and lets `ortus check` report drift
without a second parser.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import Callable, Sequence

from ortus import __version__ as ORTUS_VERSION
from ortus.core.config import DEFAULT_CODEGRAPH_MODE

TEMPLATE_PACKAGE = "ortus.templates"
BLOCK_DIR = "blocks"

#: The single claim command Ortus teaches. The bundled goal prompt spells the
#: same command, and a test asserts the two never drift apart: an agent that
#: reads AGENTS.md and an agent running under `/goal` must claim identically.
BD_CLAIM_COMMAND = "bd update <id> --status=in_progress"

#: Schema integer per block. Bump when the rendered body changes meaning; the
#: drift gate in the test suite fails a body edit that forgets to.
BLOCK_SCHEMAS: dict[str, int] = {"agents": 2, "pointer": 1}


@dataclass(frozen=True)
class ManagedFile:
    """One repo file and the block Ortus owns inside it."""

    filename: str
    block: str


#: Written for every backend. `AGENTS.md` carries the session contract;
#: `CLAUDE.md` carries a pointer at it, so a Claude session that only ever
#: loads CLAUDE.md still lands on the same rules.
MANAGED_FILES: tuple[ManagedFile, ...] = (
    ManagedFile("AGENTS.md", "agents"),
    ManagedFile("CLAUDE.md", "pointer"),
)


class AgentFileError(RuntimeError):
    """A managed file cannot be parsed or rendered.

    Carries the offending line number, because the operator's next action is
    to open the file and fix the marker by hand.
    """


class BlockOutcome(str, Enum):
    """What `apply_block` did to one file."""

    CREATED = "created"
    APPENDED = "appended"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    AHEAD = "ahead"


CODEGRAPH_SECTIONS: dict[str, str] = {
    "required": (
        "### CodeGraph\n"
        "\n"
        "CodeGraph is a prerequisite of this repo, not an enhancement. Ask it\n"
        "before grep, find, or opening files: the `codegraph_explore` MCP tool\n"
        "when it is registered, `codegraph explore \"<symbols or question>\"`\n"
        "otherwise. A missing CLI, index, or MCP capability is fatal under\n"
        "`codegraph = \"required\"` — stop and report the missing prerequisite\n"
        "instead of falling back to a slower search."
    ),
    "auto": (
        "### CodeGraph\n"
        "\n"
        "Reach for CodeGraph before grep, find, or opening files: the\n"
        "`codegraph_explore` MCP tool when it is registered, `codegraph explore\n"
        "\"<symbols or question>\"` otherwise. Under `codegraph = \"auto\"` a\n"
        "missing CLI, index, or MCP capability is not fatal — fall back to grep\n"
        "and Read and say so in the work you report."
    ),
    "off": (
        "### CodeGraph\n"
        "\n"
        "Disabled for this repo (`codegraph = \"off\"`). Use grep and Read; do\n"
        "not treat a missing index as a prerequisite failure."
    ),
}

_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")
_BEGIN_RE = re.compile(
    r"^[ \t]*<!--[ \t]*BEGIN ortus\b(?P<attrs>[^>]*?)-->[ \t]*$", re.MULTILINE
)
_END_RE = re.compile(
    r"^[ \t]*<!--[ \t]*END ortus\b(?P<attrs>[^>]*?)-->[ \t]*$", re.MULTILINE
)
_ATTR_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)=(\S+)")

# Hash-comment twins of the markers above, for files that cannot carry HTML
# comments (`.gitignore`). Same attribute grammar; `\r?` keeps a CRLF host
# file parseable without normalizing any byte outside the markers.
_HASH_BEGIN_RE = re.compile(
    r"^[ \t]*#[ \t]*BEGIN ortus\b(?P<attrs>[^\r\n]*?)\r?$", re.MULTILINE
)
_HASH_END_RE = re.compile(
    r"^[ \t]*#[ \t]*END ortus\b(?P<attrs>[^\r\n]*?)\r?$", re.MULTILINE
)


@dataclass(frozen=True)
class ParsedBlock:
    """One managed block found in a file, with enough position to splice it."""

    block: str
    schema: int
    generated_by: str
    text: str  # markers included — what `render_block` is compared against
    body: str  # between the markers
    line: int  # 1-based line of the BEGIN marker
    start: int
    end: int


def codegraph_section(mode: str) -> str:
    """The CodeGraph paragraph for `mode`, falling back to the default policy."""

    return CODEGRAPH_SECTIONS.get(mode, CODEGRAPH_SECTIONS[DEFAULT_CODEGRAPH_MODE])


def _read_block_template(block: str) -> str:
    if block not in BLOCK_SCHEMAS:
        raise AgentFileError(
            f"unknown managed block {block!r}; expected one of "
            f"{', '.join(sorted(BLOCK_SCHEMAS))}"
        )
    resource = files(TEMPLATE_PACKAGE).joinpath(BLOCK_DIR).joinpath(f"{block}.md")
    return resource.read_text(encoding="utf-8")


def block_template_source(block: str) -> str:
    """The unrendered block body; the drift gate hashes exactly this."""

    return _read_block_template(block)


def begin_marker(block: str, ortus_version: str = ORTUS_VERSION) -> str:
    return (
        f"<!-- BEGIN ortus block={block} schema={BLOCK_SCHEMAS[block]} "
        f"generated-by=ortus@{ortus_version} -->"
    )


def end_marker(block: str) -> str:
    return f"<!-- END ortus block={block} -->"


def render_block(
    block: str,
    *,
    codegraph: str = DEFAULT_CODEGRAPH_MODE,
    ortus_version: str = ORTUS_VERSION,
) -> str:
    """Render one managed block, markers included.

    Substitution is a fixed three-token vocabulary rather than a template
    engine: the bodies are Markdown carrying shell snippets, and an unknown
    `{TOKEN}` is a template bug worth failing on rather than shipping into a
    consumer repo verbatim.
    """

    body = _read_block_template(block)
    values = {
        "{CLI_VERSION}": ortus_version,
        "{BD_CLAIM_COMMAND}": BD_CLAIM_COMMAND,
        "{CODEGRAPH_SECTION}": codegraph_section(codegraph),
    }
    unknown = sorted(set(_PLACEHOLDER_RE.findall(body)) - set(values))
    if unknown:
        raise AgentFileError(
            f"block {block!r} references unknown placeholders: {', '.join(unknown)}"
        )
    for token, value in values.items():
        body = body.replace(token, value)
    return (
        f"{begin_marker(block, ortus_version)}\n"
        f"{body.strip()}\n"
        f"{end_marker(block)}"
    )


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _fail(path: Path | None, line: int, problem: str) -> AgentFileError:
    where = f"{path}:{line}" if path is not None else f"line {line}"
    return AgentFileError(f"{where}: {problem}")


def parse_blocks(text: str, *, path: Path | None = None) -> dict[str, ParsedBlock]:
    """Every managed block in `text`, keyed by block name.

    Raises :class:`AgentFileError` naming a line when the markers are
    unbalanced, nested, duplicated, or missing the attributes the writer
    needs. Aborting one file is deliberate: guessing where a half-written
    block ends is how host prose gets eaten.
    """

    return _parse_blocks(text, _BEGIN_RE, _END_RE, path=path)


def parse_hash_blocks(text: str, *, path: Path | None = None) -> dict[str, ParsedBlock]:
    """`parse_blocks` for hash-comment fences (`# BEGIN ortus ...`)."""

    return _parse_blocks(text, _HASH_BEGIN_RE, _HASH_END_RE, path=path)


def _parse_blocks(
    text: str,
    begin_re: re.Pattern[str],
    end_re: re.Pattern[str],
    *,
    path: Path | None,
) -> dict[str, ParsedBlock]:
    markers = sorted(
        [("begin", m) for m in begin_re.finditer(text)]
        + [("end", m) for m in end_re.finditer(text)],
        key=lambda item: item[1].start(),
    )
    blocks: dict[str, ParsedBlock] = {}
    open_block: str | None = None
    open_schema = 0
    open_generated_by = ""
    open_line = 0
    open_start = 0
    for kind, match in markers:
        line = _line_of(text, match.start())
        attrs = dict(_ATTR_RE.findall(match.group("attrs")))
        if kind == "begin":
            if open_block is not None:
                raise _fail(
                    path,
                    line,
                    f"BEGIN ortus marker inside block={open_block} opened at "
                    f"line {open_line}",
                )
            name = attrs.get("block")
            if not name:
                raise _fail(path, line, "BEGIN ortus marker has no block= attribute")
            if name in blocks:
                raise _fail(
                    path,
                    line,
                    f"duplicate ortus block={name}; already opened at line "
                    f"{blocks[name].line}",
                )
            raw_schema = attrs.get("schema")
            if raw_schema is None or not raw_schema.isdigit():
                raise _fail(
                    path,
                    line,
                    f"ortus block={name} has schema={raw_schema or '(missing)'}; "
                    "expected an integer",
                )
            open_block = name
            open_schema = int(raw_schema)
            open_generated_by = attrs.get("generated-by", "")
            open_line = line
            open_start = match.start()
            continue
        name = attrs.get("block")
        if open_block is None:
            raise _fail(
                path,
                line,
                f"END ortus block={name or '(unnamed)'} with no BEGIN marker",
            )
        if name != open_block:
            raise _fail(
                path,
                line,
                f"END ortus block={name or '(unnamed)'} closes block={open_block} "
                f"opened at line {open_line}",
            )
        end = match.end()
        blocks[open_block] = ParsedBlock(
            block=open_block,
            schema=open_schema,
            generated_by=open_generated_by,
            text=text[open_start:end],
            body=text[text.index("\n", open_start) + 1 : match.start()].strip("\n"),
            line=open_line,
            start=open_start,
            end=end,
        )
        open_block = None
    if open_block is not None:
        raise _fail(
            path,
            open_line,
            f"BEGIN ortus block={open_block} has no END marker",
        )
    return blocks


def read_block(path: Path, block: str) -> ParsedBlock | None:
    """The named block in `path`, or None when the file has no such block."""

    if not path.is_file():
        return None
    return parse_blocks(path.read_text(encoding="utf-8"), path=path).get(block)


def apply_block(path: Path, block: str, rendered: str) -> BlockOutcome:
    """Write `rendered` into `path`, preserving every byte outside the markers.

    Create when the file is absent, append when it carries no block of ours,
    replace in place when the block is stale, and do nothing when it already
    matches. A block written by a newer Ortus is left alone: writing our older
    body over it would silently downgrade the repo's contract.
    """

    return _apply_block(
        path, block, rendered, parse=parse_blocks, schema=BLOCK_SCHEMAS[block]
    )


def apply_hash_block(path: Path, block: str, rendered: str, *, schema: int) -> BlockOutcome:
    """`apply_block` for hash-comment fences.

    `schema` is passed in because hash blocks live outside BLOCK_SCHEMAS —
    that registry doubles as the drift gate over the markdown block templates.
    """

    return _apply_block(path, block, rendered, parse=parse_hash_blocks, schema=schema)


def _apply_block(
    path: Path,
    block: str,
    rendered: str,
    *,
    parse: Callable[..., dict[str, ParsedBlock]],
    schema: int,
) -> BlockOutcome:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        return BlockOutcome.CREATED
    text = path.read_text(encoding="utf-8")
    parsed = parse(text, path=path).get(block)
    if parsed is None:
        if text.strip():
            prefix = text if text.endswith("\n") else text + "\n"
            if not prefix.endswith("\n\n"):
                prefix += "\n"
        else:
            prefix = ""
        path.write_text(prefix + rendered + "\n", encoding="utf-8")
        return BlockOutcome.CREATED if not text.strip() else BlockOutcome.APPENDED
    if parsed.schema > schema:
        return BlockOutcome.AHEAD
    if parsed.text == rendered:
        return BlockOutcome.UNCHANGED
    path.write_text(text[: parsed.start] + rendered + text[parsed.end :], encoding="utf-8")
    return BlockOutcome.UPDATED


_IGNORE_SOURCES = (Path(".git") / "info" / "exclude", Path(".gitignore"))


def gitignore_match(repo: Path, name: str) -> str | None:
    """The last ignore pattern that would exclude top-level `name`, if any.

    Deliberately a small matcher over the repo's own ignore files rather than
    a `git check-ignore` subprocess: `ortus check` is strictly read-only and
    must not spawn processes, and the patterns that matter here — `AGENTS.md`,
    `/AGENTS.md`, `*.md`, and their `!` negations — are exactly what a repo
    reaches for when it decides an agent file is generated scratch.
    """

    verdict: str | None = None
    for relative in _IGNORE_SOURCES:
        source = repo / relative
        if not source.is_file():
            continue
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            pattern = (line[1:] if negated else line).rstrip("/")
            candidates = {pattern, pattern.lstrip("/"), pattern.removeprefix("**/")}
            if any(fnmatch.fnmatch(name, candidate) for candidate in candidates):
                verdict = None if negated else line
    return verdict


_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(?P<title>.+?)[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")


def _normalized_heading(value: str) -> str:
    """Case- and punctuation-insensitive heading key (mirrors readiness)."""

    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _atx_headings(text: str) -> list[str]:
    """ATX heading titles in `text`, in order, skipping fenced code.

    Fences matter because the managed bodies carry shell snippets whose `#`
    comment lines would otherwise read as level-one headings. Setext headings
    (underlined with === or ---) are out of scope: every legacy whole-file
    render that motivates this detector used ATX.
    """

    titles: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        opened = _FENCE_RE.match(line)
        if opened:
            marker = opened.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        heading = _ATX_HEADING_RE.match(line)
        if heading:
            titles.append(heading.group("title"))
    return titles


def duplicated_headings(text: str, *, path: Path | None = None) -> tuple[str, ...]:
    """Managed-block headings that host prose outside the markers repeats.

    Compares normalized headings in the host prose against the union of every
    managed block present in `text` — the field case is a pre-marker render
    left above the block by a forced re-init, which agents then read first.
    Each heading is reported once, in block order, under the block's own
    spelling. A file with no markers has nothing to compare and yields
    nothing. Detection only: callers warn, and host prose stays untouched no
    matter what it duplicates.
    """

    blocks = parse_blocks(text, path=path)
    if not blocks:
        return ()
    host = text
    for parsed in sorted(blocks.values(), key=lambda b: b.start, reverse=True):
        host = host[: parsed.start] + host[parsed.end :]
    outside = {_normalized_heading(title) for title in _atx_headings(host)}
    outside.discard("")
    duplicated: list[str] = []
    seen: set[str] = set()
    for parsed in sorted(blocks.values(), key=lambda b: b.start):
        for title in _atx_headings(parsed.body):
            key = _normalized_heading(title)
            if key and key in outside and key not in seen:
                seen.add(key)
                duplicated.append(title)
    return tuple(duplicated)


def duplicate_headings_message(filename: str, headings: Sequence[str]) -> str:
    """The one warning line `ortus init` prints and `ortus check` reports."""

    return (
        f"{filename} host prose duplicates managed-block headings: "
        f"{', '.join(headings)} — delete the stale copies outside the ortus markers"
    )
