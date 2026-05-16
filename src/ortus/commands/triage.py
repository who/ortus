"""ortus triage <repo> — interactive bd-issue triage session.

Full implementation lands in idzn.2 (Phase 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented
from ortus.core.repo import resolve_repo


def triage(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Triage open bd issues interactively (claude-mediated)."""
    resolve_repo(repo)
    not_implemented("triage", "idzn.2")
