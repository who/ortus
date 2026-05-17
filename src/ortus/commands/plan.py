"""ortus plan <repo> [<PRD>] — decompose a PRD (or freeform idea) into bd issues.

Ports ortus/idea.sh's PRD-intake and idea-expansion flows. Explicit <repo>
arg eliminates the cd-to-PRD-dir bug from idea.sh (FR-014).
"""

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
    """Indirection so tests can swap in a fake claude binary."""
    return ClaudeRunner()


def _decompose_prd(repo: Path, prd: Path, *, log_path: Path) -> int:
    """Run claude with the plan prompt, expanded to reference the PRD path."""
    prompt = resolve_prompt("plan-prompt", repo=repo).text
    # The plan-prompt uses literal "$prd_path" as a placeholder for the absolute
    # PRD path; substitute it before handing to claude.
    expanded = prompt.replace("$prd_path", str(prd.resolve()))
    runner = _make_runner()
    return runner.run(expanded, repo=repo, log_path=log_path)


def _expand_idea(repo: Path, *, log_path: Path) -> int:
    """Run the interactive idea-expansion flow (interview→PRD→tasks)."""
    # Use the grind prompt's interview entry-point indirectly; for now we
    # just hand claude a freeform "interview the user about their idea"
    # instruction. Phase 3 idzn.1 fleshes out the full interview prompt.
    runner = _make_runner()
    prompt = (
        "The user invoked `ortus plan <repo>` without a PRD. Run an interactive "
        "idea-expansion interview: ask 3-7 questions to clarify the goal, then "
        "draft a brief PRD inline, then call `bd create` for each work item. "
        "End the turn when bd ready shows the new issues."
    )
    return runner.run(prompt, repo=repo, log_path=log_path)


def plan(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    prd: Optional[Path] = typer.Argument(
        None, help="Optional PRD path. If omitted, runs the idea-interview flow."
    ),
) -> None:
    """Decompose a PRD into bd issues, or interview-then-PRD-then-decompose."""
    target = resolve_repo(repo)

    if prd is not None and not prd.is_file():
        output.error(f"PRD not found at {prd}")
        raise typer.Exit(code=1)

    log_dir = target / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "plan.log"

    client = BdClient(target)
    before = {i["id"] for i in client.list_open()}

    rc = _decompose_prd(target, prd, log_path=log_path) if prd else _expand_idea(target, log_path=log_path)
    if rc != 0:
        output.error(f"plan failed (claude exit {rc}); see {log_path}")
        raise typer.Exit(code=rc)

    after = client.list_open()
    new_ids = [i["id"] for i in after if i["id"] not in before]

    if not new_ids:
        output.warn("plan ran but no new issues were created in this workspace")
        return

    output.success(f"plan created {len(new_ids)} issue(s) in {target}/.beads/")
    output.table(
        ["id", "type", "priority", "title"],
        [
            (i["id"], i["issue_type"], f"P{i['priority']}", i["title"])
            for i in after
            if i["id"] in set(new_ids)
        ],
    )
    output.info(f"\nNext: [bold]ortus grind {target}[/bold]")
