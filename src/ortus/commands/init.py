"""ortus init <repo> — bootstrap a fresh repo with bd + .claude + AGENTS.md.

Full implementation lands in q075.5. This stub is callable but exits 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented


def init(
    repo: Optional[Path] = typer.Argument(
        None,
        help="Target repo directory. Defaults to $PWD; no walk-up.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run init on a repo that's already bootstrapped."
    ),
    prefix: Optional[str] = typer.Option(
        None, "--prefix", help="Override the bd issue-id prefix (default: dir name)."
    ),
) -> None:
    """Bootstrap a new repo: bd workspace, .claude/settings.json, .ortusrc, AGENTS.md."""
    not_implemented("init", "q075.5")
