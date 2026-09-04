"""Rich-based output formatters (NFR-005).

Stderr for warnings/errors; stdout for success/info/table. All callers go
through these helpers so styling stays consistent across verbs.

Caller text is escaped here, not at call sites: Rich reads `[local]` as a
style tag and drops it, so a message naming a TOML table would otherwise reach
the terminal with a hole in it. The coloured prefixes are the helpers' own
markup and stay live.
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable

from rich.console import Console
from rich.markup import escape as _escape_markup
from rich.table import Table
from rich.text import Text

_out = Console()
_err = Console(stderr=True)


def info(message: str) -> None:
    _out.print(message)


def success(message: str) -> None:
    _out.print(f"[green]✓[/green] {_escape_markup(message)}")


def warn(message: str) -> None:
    _err.print(f"[yellow]warn:[/yellow] {_escape_markup(message)}")


def error(message: str, *, hint: str | None = None) -> None:
    _err.print(f"[red]error:[/red] {_escape_markup(message)}")
    if hint:
        _err.print(f"       {_escape_markup(hint)}")


def note(message: str) -> None:
    """A plain stderr line: a menu row, a prompt's lead-in, a nudge to retry.

    No prefix, because the line is not a verdict; stderr, because a caller
    capturing stdout must find the result there and nothing else.
    """
    _err.print(_escape_markup(message), highlight=False)


def progress(verb: str, phase: str) -> None:
    """Emit a per-phase progress line to stderr in the canonical CLI format.

    Format: `[YYYY-MM-DD HH:MM:SS] <phase>`. See AGENTS.md "CLI output
    convention" — silence-equals-hung is the perceived default, so every
    non-trivial phase of a non-interactive verb must call this so the operator
    sees motion. The operator invoked the verb themselves, so the old
    `[ortus <verb>]` tag was per-line reading tax and is gone (ortus-kawu);
    `verb` stays in the signature so call sites keep naming their channel. The
    timestamp is local time in the same shape the grind log file writes, so
    console and log lines can be compared without mental reformatting.
    """
    del verb  # kept for call-site uniformity; no longer rendered
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_phase = _escape_markup(phase)
    _err.print(
        f"[dim]\\[{stamp}][/dim] {safe_phase}",
        highlight=False,
    )


def table(headers: Iterable[str], rows: Iterable[Iterable[str | Text]]) -> None:
    """Render rows on stdout with caller text printed literally.

    Cells are caller text and are escaped, so a `[local]` row name or a detail
    quoting a TOML table survives Rich. A `Text` cell is a styled renderable
    the caller built on purpose (the status glyph in `ortus check`) and passes
    through untouched. Headers are ortus-owned literals.
    """
    t = Table()
    for h in headers:
        t.add_column(h)
    for row in rows:
        t.add_row(*[c if isinstance(c, Text) else _escape_markup(str(c)) for c in row])
    _out.print(t)
