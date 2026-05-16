"""Rich-based output formatters (NFR-005).

Stderr for warnings/errors; stdout for success/info/table. All callers go
through these helpers so styling stays consistent across verbs.
"""

from __future__ import annotations

from typing import Iterable

from rich.console import Console
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


def table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> None:
    t = Table()
    for h in headers:
        t.add_column(h)
    for row in rows:
        t.add_row(*[str(c) for c in row])
    _out.print(t)
