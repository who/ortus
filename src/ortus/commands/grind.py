"""ortus grind <repo> — autonomous /goal-directive orchestrator loop.

Full implementation lands in xvel.4 (Phase 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented
from ortus.core.repo import resolve_repo


def grind(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    tasks: Optional[int] = typer.Option(
        None, "--tasks", help="Stop after N tasks complete (default: drain queue)."
    ),
    condition: Optional[str] = typer.Option(
        None, "-c", "--condition", help="Custom /goal condition (overrides default)."
    ),
    docker: bool = typer.Option(
        False, "--docker", help="Run inside a docker sandbox instead of bwrap."
    ),
) -> None:
    """Drive the bd queue via a long-lived claude -p /goal session."""
    resolve_repo(repo)
    not_implemented("grind", "xvel.4")
