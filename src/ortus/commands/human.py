"""ortus human <repo> — emit HUMAN-TODO.md for items needing a human decision.

Full implementation lands in idzn.3 (Phase 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented
from ortus.core.repo import resolve_repo


def human(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Render HUMAN-TODO.md from bd issues flagged for a human decision."""
    resolve_repo(repo)
    not_implemented("human", "idzn.3")
