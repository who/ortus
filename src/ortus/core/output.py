"""Rich-based output formatters (NFR-005).

Stderr for warnings/errors; stdout for success/info/table. All callers go
through these helpers so styling stays consistent across verbs.
"""

from __future__ import annotations

from typing import Iterable

from rich.console import Console
from rich.markup import escape as _escape_markup
from rich.table import Table

_out = Console()
_err = Console(stderr=True)


def info(message: str) -> None:
    _out.print(message)


def success(message: str) -> None:
    _out.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    _err.print(f"[yellow]warn:[/yellow] {message}")


def error(message: str, *, hint: str | None = None) -> None:
    _err.print(f"[red]error:[/red] {message}")
    if hint:
        _err.print(f"       {hint}")


def progress(verb: str, phase: str) -> None:
    """Emit a per-phase progress line to stderr in the canonical CLI format.

    Format: `[ortus <verb>] <phase>`. See AGENTS.md "CLI output convention" —
    silence-equals-hung is the perceived default, so every non-trivial phase
    of a non-interactive verb must call this so the operator sees motion.
    """
    safe_verb = _escape_markup(verb)
    safe_phase = _escape_markup(phase)
    _err.print(f"[dim]\\[ortus {safe_verb}][/dim] {safe_phase}", highlight=False)


def table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> None:
    t = Table()
    for h in headers:
        t.add_column(h)
    for row in rows:
        t.add_row(*[str(c) for c in row])
    _out.print(t)
