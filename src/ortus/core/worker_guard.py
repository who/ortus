"""The Bash-command check a grind worker's PreToolUse hook runs.

A worker that has pushed `main` must not rewrite it. Two workers read the
Stop hook's "only an over-long subject is repaired in place" as permission to
amend a pushed commit and force-push it, and nothing refused. This module
finds the git invocations in one Bash command line and names the one that
would rewrite published history: a forced push, or an amend of a commit a
remote-tracking ref already contains. Everything else passes, including any
command line this cannot parse — the hook is a backstop, not a shell.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

#: Set by grind on every worker it launches; the worker hook acts only then.
GRIND_WORKER_ENV = "ORTUS_GRIND_WORKER"

#: The operator's escape hatch: a run that must rewrite history sets this.
ALLOW_FORCE_PUSH_ENV = "ORTUS_ALLOW_FORCE_PUSH"

#: Shell control operators a command line is split on before tokenising.
_SEPARATORS = re.compile(r"&&|\|\||[;|\n]")

#: `git push` long options that overwrite the remote ref unconditionally or
#: conditionally; either way a rewrite of what origin already has.
_FORCE_LONG = ("--force", "--force-with-lease", "--force-if-includes", "--mirror")

#: `git` global options that take a separate value, so the subcommand is
#: found after it rather than mistaken for it.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

FINAL_COMMIT_MESSAGE = (
    "a pushed commit is final. Make a new commit instead, or leave the message "
    "as it is and say so in your answer; ask the operator if history must change."
)


def _git_invocations(command: str) -> list[tuple[str | None, list[str]]]:
    """Each `git` call in `command`: its `-C` directory and the args after `git`."""
    calls: list[tuple[str | None, list[str]]] = []
    for segment in _SEPARATORS.split(command):
        tokens = shlex.split(segment)
        # Leading `VAR=value` assignments are environment, not the command.
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if not tokens or Path(tokens[0]).name != "git":
            continue
        args = tokens[1:]
        directory: str | None = None
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in _GIT_VALUE_OPTIONS and args:
                value = args.pop(0)
                if option == "-C":
                    directory = value
        calls.append((directory, args))
    return calls


def _forced_push(args: list[str]) -> str | None:
    for arg in args[1:]:
        if arg == "--":
            continue
        if arg.split("=", 1)[0] in _FORCE_LONG:
            return arg
        if arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
            return arg
        if arg.startswith("+"):
            return arg
    return None


def _head_is_pushed(repo: Path) -> bool:
    """True when a remote-tracking ref already contains HEAD."""
    try:
        proc = subprocess.run(
            ["git", "branch", "-r", "--contains", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def bash_violation(command: str, repo: Path) -> str | None:
    """Why `command` would rewrite pushed history, or None when it would not."""
    try:
        calls = _git_invocations(command)
    except ValueError:
        return None
    for directory, args in calls:
        if not args:
            continue
        if args[0] == "push":
            flag = _forced_push(args)
            if flag is not None:
                return f"`git push {flag}` refused: {FINAL_COMMIT_MESSAGE}"
        elif args[0] == "commit" and "--amend" in args[1:]:
            where = repo / directory if directory else repo
            if _head_is_pushed(where):
                return (
                    "`git commit --amend` refused: HEAD is already on a "
                    f"remote-tracking ref; {FINAL_COMMIT_MESSAGE}"
                )
    return None
