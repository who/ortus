"""ortus triage <repo> — interactive triage of bd human-flagged issues (idzn.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.bd import BdClient
from ortus.core.claude import ClaudeRunner
from ortus.core.prompts import resolve_prompt
from ortus.core.repo import resolve_repo


def _make_runner() -> ClaudeRunner:
    return ClaudeRunner()


def triage(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Triage open bd issues interactively (claude-mediated)."""
    target = resolve_repo(repo)
    client = BdClient(target)
    flagged = client.list_human()
    if not flagged:
        output.info("no human-queue items — nothing to triage")
        return

    prompt = resolve_prompt("triage-prompt", repo=target).text

    log_path = target / "logs" / "triage.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    runner = _make_runner()
    output.info(
        f"triage starting; {len(flagged)} human-flagged issue(s); log → {log_path.relative_to(target)}"
    )
    rc = runner.run(prompt, repo=target, log_path=log_path)
    if rc != 0:
        output.error(f"triage exited {rc}; see {log_path}")
        raise typer.Exit(code=rc)
    output.success("triage complete")
