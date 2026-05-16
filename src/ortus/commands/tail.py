"""ortus tail <repo> — follow logs/grind-*.log + logs/goal-*.log + logs/ralph-*.log.

Full implementation lands in idzn.4 (Phase 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented
from ortus.core.repo import resolve_repo


def tail(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    follow: bool = typer.Option(
        True, "--follow/--no-follow", help="Stream new log lines (default: on)."
    ),
) -> None:
    """Tail orchestrator log files (grind-*, goal-*, ralph-*)."""
    resolve_repo(repo)
    not_implemented("tail", "idzn.4")
