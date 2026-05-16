"""ortus check <repo> — verify prerequisites for the orchestrator.

Full implementation lands in q075.6.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented


def check(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Verify bd/claude/sandbox prereqs and hook-disable state."""
    not_implemented("check", "q075.6")
