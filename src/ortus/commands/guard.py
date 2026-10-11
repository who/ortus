"""`ortus guard`: the checks the stealth git hooks run.

Plumbing for the `pre-push` and `pre-commit` hooks a stealth init installs,
not a verb an operator types. Each subcommand prints every violation and
exits 1, or exits 0 silently. Outside stealth both always exit 0, so a hook
left behind in a repository that has dropped stealth blocks nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from ortus.core.stealth import is_stealth, push_violations, staged_gitignore_violation

guard_app = typer.Typer(
    name="guard",
    help="Stealth git-hook checks (run by the hooks ortus init --stealth installs).",
    no_args_is_help=True,
)


def _refuse(kind: str, violations: list[str]) -> None:
    for violation in violations:
        typer.echo(f"ortus stealth guard: {violation}", err=True)
    typer.echo(
        f"ortus stealth guard: {kind} refused; nothing was rewritten, fix the "
        "commits or paths above by hand",
        err=True,
    )
    raise typer.Exit(code=1)


@guard_app.command("pre-push")
def pre_push(
    remote: str = typer.Argument(..., help="Remote name (or URL) git is pushing to."),
    url: str = typer.Argument("", help="The remote's URL, as git passes it."),
) -> None:
    """Refuse a stealth push that would leak Ortus paths, ids or tracker refs."""
    repo = Path.cwd()
    if not is_stealth(repo):
        return
    refspecs = []
    for line in sys.stdin.read().splitlines():
        fields = line.split()
        if len(fields) == 4:
            refspecs.append((fields[0], fields[1], fields[2], fields[3]))
    violations = push_violations(repo, remote, refspecs, url=url or None)
    if violations:
        _refuse("push", violations)


@guard_app.command("pre-commit")
def pre_commit() -> None:
    """Refuse a stealth commit that stages a `.gitignore`."""
    repo = Path.cwd()
    if not is_stealth(repo):
        return
    violation = staged_gitignore_violation(repo, os.environ)
    if violation:
        _refuse("commit", [violation])
