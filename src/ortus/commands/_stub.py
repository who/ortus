"""Shared 'not implemented' helper for Phase 1 stub verbs."""

from __future__ import annotations

import typer
from rich.console import Console

_err = Console(stderr=True)


def not_implemented(verb: str, phase: str) -> None:
    """Print a clear unimplemented-stub message and exit 2."""
    _err.print(
        f"[yellow]ortus {verb}:[/yellow] not implemented in this phase "
        f"(lands in {phase})."
    )
    raise typer.Exit(code=2)
