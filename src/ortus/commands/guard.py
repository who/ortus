"""`ortus guard`: the checks the stealth git hooks and worker hooks run.

Plumbing for the `pre-push` and `pre-commit` hooks a stealth init installs,
not a verb an operator types. Each subcommand prints every violation and
exits 1, or exits 0 silently. Outside stealth both always exit 0, so a hook
left behind in a repository that has dropped stealth blocks nothing.

`commit-msg` is the hook `ortus init` installs in every repository: it
refuses a commit whose subject is over 72 characters, stealth or not.

`worker-bash` is the Claude Code PreToolUse hook `ortus init` renders. It
refuses a grind worker's forced push, amend of a pushed commit, or
`git commit -m` with an over-long subject with exit 2, which Claude Code feeds
back to the model, and allows everything outside a grind worker.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from ortus.core.stealth import is_stealth, push_violations, staged_gitignore_violation
from ortus.core.worker_guard import (
    ALLOW_FORCE_PUSH_ENV,
    ALLOW_LONG_SUBJECT_ENV,
    GRIND_WORKER_ENV,
    bash_violation,
    commit_subject_violation,
    subject_violation,
)

guard_app = typer.Typer(
    name="guard",
    help=(
        "Stealth git-hook checks and grind-worker command checks (run by the "
        "hooks ortus init installs)."
    ),
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


@guard_app.command("commit-msg")
def commit_msg(
    message_file: Path = typer.Argument(..., help="The message file git passes the hook."),
) -> None:
    """Refuse a commit whose subject is over 72 characters."""
    if os.environ.get(ALLOW_LONG_SUBJECT_ENV) == "1":
        return
    try:
        message = message_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    violation = subject_violation(message)
    if violation:
        typer.echo(f"ortus guard: {violation}", err=True)
        raise typer.Exit(code=1)


@guard_app.command("worker-bash")
def worker_bash() -> None:
    """Refuse a grind worker's history rewrite or over-long `git commit -m`."""
    if os.environ.get(GRIND_WORKER_ENV) != "1":
        return
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except ValueError:
        return
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return
    if os.environ.get(ALLOW_LONG_SUBJECT_ENV) != "1":
        long_subject = commit_subject_violation(command)
        if long_subject:
            typer.echo(f"ortus guard: {long_subject}", err=True)
            raise typer.Exit(code=2)
    if os.environ.get(ALLOW_FORCE_PUSH_ENV) == "1":
        return
    cwd = payload.get("cwd")
    repo = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
    violation = bash_violation(command, repo)
    if violation:
        typer.echo(f"ortus worker guard: {violation}", err=True)
        raise typer.Exit(code=2)
