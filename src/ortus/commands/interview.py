"""ortus interview <repo> — interactive PRD-building interview.

Full implementation lands in idzn.1 (Phase 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented
from ortus.core.repo import resolve_repo


def interview(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Run an interactive interview to draft a PRD."""
    resolve_repo(repo)
    not_implemented("interview", "idzn.1")
