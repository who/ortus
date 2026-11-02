"""The Bash-command check a grind worker's PreToolUse hook runs.

A worker that has pushed `main` must not rewrite it. Two workers read the
Stop hook's "only an over-long subject is repaired in place" as permission to
amend a pushed commit and force-push it, and nothing refused. This module
finds the git invocations in one Bash command line and names the one that
would rewrite published history: a forced push, or an amend of a commit a
remote-tracking ref already contains. Everything else passes, including any
command line this cannot parse — the hook is a backstop, not a shell.

It also owns the commit-subject limit: `subject_violation` is what the
`commit-msg` hook `ortus init` installs runs on the message file, and
`commit_subject_violation` applies the same rule to a worker's
`git commit -m` before the commit exists.
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

#: The longest commit subject accepted, counted with the `<issue-id>: ` prefix.
SUBJECT_LIMIT = 72

#: The operator's escape hatch for one deliberately long subject.
ALLOW_LONG_SUBJECT_ENV = "ORTUS_ALLOW_LONG_SUBJECT"

#: Subjects git itself writes, which the limit leaves alone.
_GENERATED_SUBJECT = re.compile(r'^(Merge |fixup! |squash! |amend! |Revert ")')

#: The heredoc spelling agents use for a multi-line `-m`:
#: `-m "$(cat <<'EOF' ... EOF)"`, whose body is the message itself.
_HEREDOC_MESSAGE = re.compile(
    r"^\$\(cat\s+<<-?\s*(['\"]?)(?P<tag>\w+)\1[ \t]*\n(?P<body>.*?)\n\s*(?P=tag)\s*\)$",
    re.DOTALL,
)

#: Marker lines around the `commit-msg` section `ortus init` writes, so a
#: re-install replaces its own section and keeps whatever else the hook holds.
COMMIT_MSG_BEGIN = "# --- BEGIN ORTUS COMMIT-MSG v1 ---"
COMMIT_MSG_END = "# --- END ORTUS COMMIT-MSG v1 ---"
_COMMIT_MSG_SECTION = (
    f"{COMMIT_MSG_BEGIN}\n"
    "if command -v ortus >/dev/null 2>&1; then\n"
    '  ortus guard commit-msg "$1" || exit $?\n'
    "fi\n"
    f"{COMMIT_MSG_END}\n"
)

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


def commit_subject(message: str) -> str:
    """The subject git will record: the first non-blank, non-comment line."""
    for line in message.splitlines():
        if line.startswith("#"):
            continue
        if line.strip():
            return line.strip()
    return ""


def subject_violation(message: str) -> str | None:
    """Why `message`'s subject is over the limit, or None when it is not."""
    subject = commit_subject(message)
    if len(subject) <= SUBJECT_LIMIT or _GENERATED_SUBJECT.match(subject):
        return None
    return (
        f"commit subject is {len(subject)} characters; the limit is {SUBJECT_LIMIT} "
        "(issue-id prefix included). Shorten it and commit again."
    )


def _commit_message_arg(args: list[str]) -> str | None:
    """The first `-m`/`--message` value in `git commit` args, or None."""
    rest = iter(args[1:])
    for arg in rest:
        if arg == "--":
            return None
        if arg == "--message":
            return next(rest, None)
        if arg.startswith("--message="):
            return arg.split("=", 1)[1]
        if arg.startswith("--") or not arg.startswith("-"):
            continue
        cluster = arg[1:]
        for index, flag in enumerate(cluster):
            if flag == "m":
                value = cluster[index + 1 :]
                return value if value else next(rest, None)
            if flag in "FCct":
                if not cluster[index + 1 :]:
                    next(rest, None)
                break
    return None


def _shell_git_calls(command: str) -> list[list[str]]:
    """Args after each `git` in `command`, keeping quoted newlines intact."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    calls: list[list[str]] = []
    current: list[str] | None = None
    for token in lexer:
        if token and set(token) <= set(";&|"):
            current = None
        elif Path(token).name == "git":
            current = []
            calls.append(current)
        elif current is not None:
            current.append(token)
    return calls


def commit_subject_violation(command: str) -> str | None:
    """Why a `git commit -m` in `command` has an over-long subject, or None."""
    try:
        calls = _shell_git_calls(command)
    except ValueError:
        return None
    for args in calls:
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in _GIT_VALUE_OPTIONS and args:
                args.pop(0)
        if not args or args[0] != "commit":
            continue
        message = _commit_message_arg(args)
        if message is None:
            continue
        heredoc = _HEREDOC_MESSAGE.match(message)
        if heredoc is not None:
            message = heredoc.group("body")
        elif "$(" in message or "`" in message:
            continue  # a substitution this cannot evaluate; the hook sees it
        violation = subject_violation(message)
        if violation:
            return violation
    return None


def install_commit_msg_hook(hooks: Path) -> Path:
    """Write Ortus's marked `commit-msg` section into `hooks`; return the hook.

    A hook that already exists keeps every line outside the markers, the way
    bd keeps content outside its own section, and an earlier Ortus section is
    replaced in place rather than appended twice.
    """
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "commit-msg"
    text = hook.read_text(encoding="utf-8", errors="replace") if hook.exists() else ""
    if COMMIT_MSG_BEGIN in text and COMMIT_MSG_END in text:
        head, _, tail = text.partition(COMMIT_MSG_BEGIN)
        _, _, tail = tail.partition(COMMIT_MSG_END)
        text = head + _COMMIT_MSG_SECTION + tail.lstrip("\n")
    elif text:
        text = text.rstrip("\n") + "\n\n" + _COMMIT_MSG_SECTION
    else:
        text = "#!/bin/sh\n" + _COMMIT_MSG_SECTION
    hook.write_text(text, encoding="utf-8")
    hook.chmod(0o755)
    return hook
