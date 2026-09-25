"""Supplemental tool policy. An allow result still requires existing permissions.

Local policy stops a short, enumerated list of clearly irreversible calls, plus
a tracker call whose exit status the shell would swallow, before any client is
constructed. Everything else — compound shell, version control commands, test
runners, search and MCP tools — becomes one bounded request whose typed answers are read as a probability vector over allow,
deny_call and park_bead, with the argmax taken in code. There is no confidence
floor and no human-need threshold anywhere in this module: an unsure answer
flattens toward uniform and resolves to the class the call is already in, which
is allow. Unfamiliar is not the same as irreversible.

This module never executes shell text, reads target contents, or replaces the
execution sandbox. Splitting a command into words is inspection only.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from ortus.core.user_env import judge_environment

from ortus.core.judge import FailureMode, JudgeConfig, JudgeState
from ortus.core.judge_state import StateError, sanitize_field
from ortus.core.judge_typesafe import (
    ACTION_RISK, API_KEY_ENV, NEEDS_HUMAN, JudgeFailure, JudgeUsage, _answer,
    _default_client, _Invalid, _mapping, _number, _usage, build_questions,
)

MAX_INPUT_BYTES = 65536
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)
_SECRET_FILES = {"credentials", "credentials.json", "id_rsa", "id_ed25519", "auth.json"}
_SECRET_DIRS = {".ssh", ".aws", ".gnupg"}

#: Top of the action-risk rubric, so a score becomes a fraction of the scale
#: rather than a number compared against a band.
_RISK_CEILING = 2.0

#: Longest single argument value repeated verbatim into a summary. Anything
#: longer is described by its shape, so a tool's payload never travels.
_VALUE_CAP = 120

#: Argument names that carry what a tool writes rather than what it acts on.
#: Their shape is summarized and their text never is, whatever its length: a
#: judge decides on the target of a call, not on its contents.
_OPAQUE_KEYS = frozenset({
    "content", "new_string", "old_string", "new_source", "old_source",
    "prompt", "body", "text", "input", "source", "stdin", "patch",
})

#: Shell operators a command is split on before each segment is read as one
#: simple command. A pipeline is several calls and any of them may be the
#: destructive one.
_SEPARATORS = re.compile(r"\|\||&&|[;|&\n\r]")

#: A leading `NAME=value` word, which prefixes a command rather than being one.
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=", re.S)

#: Leading words that run another command and would hide it from a naive
#: argv[0] read. Stripping them is what makes `sudo rm -rf /` the same call as
#: `rm -rf /` to the checks below.
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "command", "nohup", "time", "nice", "exec", "stdbuf",
})

#: Programs whose own arguments are another command line. A call handed to one
#: of these never appears as a segment's argv[0], so their words are read again
#: rather than taken for operands.
_COMMAND_RUNNERS = frozenset({
    "xargs", "bash", "sh", "zsh", "dash", "ksh", "parallel",
})

#: The `rm` flags that traverse a directory rather than unlink one entry.
_RECURSIVE = frozenset({"--recursive", "-r", "-R"})

#: git flags that take a value, so the subcommand is not the word after them.
_GIT_VALUE_FLAGS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
})

#: The push flags that overwrite a published history rather than extend it.
_FORCE_PUSH = frozenset({
    "-f", "--force", "--force-with-lease", "--force-if-includes",
})


class ToolAction(str, Enum):
    ALLOW = "allow"
    DENY_CALL = "deny_call"
    PARK_BEAD = "park_bead"


#: Argmax order, least drastic first. Ties resolve to the earliest entry, so a
#: flat or unconfident vector allows the call rather than refusing it or
#: spending an operator's attention on the bead behind it.
_ACTIONS: tuple[ToolAction, ...] = (
    ToolAction.ALLOW, ToolAction.DENY_CALL, ToolAction.PARK_BEAD,
)


@dataclass(frozen=True)
class ToolDecision:
    """One tool outcome plus whatever produced it, for the hook and the log.

    A local refusal carries its enumerated reason and no vector. A judged call
    carries the vector it was read off, so the record says which of the two
    decided and a replay can re-take the argmax without a second request.
    """

    action: ToolAction
    reason: str
    p_allow: float | None = None
    p_deny_call: float | None = None
    p_park_bead: float | None = None
    failure: JudgeFailure | None = None
    latency_ms: float = 0.0
    usage: JudgeUsage | None = None

    def vector(self) -> dict[str, float] | None:
        """The probability vector keyed by action value, or None for local policy."""
        if self.p_allow is None or self.p_deny_call is None or self.p_park_bead is None:
            return None
        return {
            ToolAction.ALLOW.value: self.p_allow,
            ToolAction.DENY_CALL.value: self.p_deny_call,
            ToolAction.PARK_BEAD.value: self.p_park_bead,
        }


@dataclass(frozen=True)
class ToolInput:
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolInspection:
    decision: ToolDecision | None
    # Only locally screened fields, never the original arguments.
    summary: str = ""


def _stop(reason: str) -> ToolInspection:
    """Every local refusal denies this one call; only a judgment parks a bead."""
    return ToolInspection(ToolDecision(ToolAction.DENY_CALL, reason))


def _secret_path(path: Path) -> bool:
    return any(
        part in _SECRET_DIRS or part in _SECRET_FILES
        or part == ".env" or part.startswith(".env.")
        or part.endswith((".pem", ".key"))
        for part in path.parts
    )


def _segments(command: str) -> list[list[str]]:
    """The command's segments, each split into words. Nothing is executed.

    A segment that will not tokenize falls back to a whitespace split rather
    than stopping the call: an opaque grammar is a reason to read it loosely,
    not a reason to hand the worker's next step to an operator.
    """
    found: list[list[str]] = []
    for part in _SEPARATORS.split(command):
        stripped = part.strip()
        if not stripped:
            continue
        try:
            words = shlex.split(stripped, posix=True)
        except ValueError:
            words = stripped.split()
        if words:
            found.append(words)
    return found


def _unwrap(argv: list[str]) -> list[str]:
    """Drop leading environment assignments and command wrappers."""
    index = 0
    while index < len(argv) and (
        _ASSIGNMENT.match(argv[index]) or PurePosixPath(argv[index]).name in _WRAPPERS
    ):
        index += 1
    return argv[index:]


def _operands(argv: list[str]) -> tuple[set[str], list[str]]:
    """One simple command's flags and its positional words, honouring ``--``."""
    flags: set[str] = set()
    operands: list[str] = []
    options = True
    for word in argv[1:]:
        if options and word == "--":
            options = False
        elif options and len(word) > 1 and word.startswith("-"):
            flags.add(word)
        else:
            operands.append(word)
    return flags, operands


def _delete_reason(
    argv: list[str], root: Path, roots: tuple[Path, ...], home: Path,
) -> str | None:
    """The enumerated refusals for a delete: roots, home, and escapes."""
    flags, operands = _operands(argv)
    recursive = any(
        flag in _RECURSIVE
        or (not flag.startswith("--") and ("r" in flag[1:] or "R" in flag[1:]))
        for flag in flags
    )
    for operand in operands:
        try:
            lexical = root / operand
            resolved = lexical.resolve()
        except (OSError, RuntimeError, ValueError):
            # A path this process cannot resolve is one it cannot prove safe.
            return "unresolved_delete"
        # Deny lexical roots too: on macOS `/tmp/..` resolves through the /tmp
        # symlink to /private rather than to /.
        if (resolved == Path("/") or Path(os.path.normpath(lexical)) == Path("/")
                or resolved == root or resolved == home):
            return "recursive_root_deletion" if recursive else "delete_outside_roots"
        if not any(resolved.is_relative_to(allowed) for allowed in roots):
            return "delete_outside_roots"
    return None


def _git_reason(argv: list[str]) -> str | None:
    """Force-push and hard-reset, the two git calls that destroy history.

    Neither is refused by branch: this process has no branch context and the
    conservative member of the pair cannot be wrong in the direction that
    costs work. Ordinary version control — status, diff, add, commit, an
    unforced push — is not matched here and is judged like any other call.
    """
    index = 1
    while index < len(argv):
        word = argv[index]
        if word in _GIT_VALUE_FLAGS:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            break
    if index >= len(argv):
        return None
    flags, _ = _operands(argv[index:])
    if argv[index] == "push" and (
        flags & _FORCE_PUSH
        or any(flag.startswith("--force-with-lease=") for flag in flags)
        or any(not flag.startswith("--") and "f" in flag[1:] for flag in flags)
    ):
        return "force_push"
    if argv[index] == "reset" and "--hard" in flags:
        return "hard_reset"
    return None


def _runs_bd(word: str) -> bool:
    """Whether one word handed to a command runner is itself a `bd` line."""
    for words in _segments(word):
        argv = _unwrap(words)
        if argv and PurePosixPath(argv[0]).name == "bd":
            return True
    return False


def wrapped_bd_reason(command: str) -> str | None:
    """The refusal for a tracker call that is not the whole command.

    A `bd` invocation inside a pipeline, a compound line or another program
    reports someone else's exit status: a pipeline yields its last stage's and
    `xargs` yields its own, so a claim or close that failed reads as a success
    to the worker that made it. The issue record is what a lost window is
    recovered from, and a bare `bd ...` line is the only shape whose failure is
    visible, so every other one stops here.

    Leading assignments and wrappers are stripped exactly as the destructive
    checks strip them, which makes `sudo bd close x | tee log` the same call as
    `bd close x | tee log`.
    """
    segments = _segments(command)
    for words in segments:
        argv = _unwrap(words)
        if not argv:
            continue
        executable = PurePosixPath(argv[0]).name
        if executable == "bd":
            if len(segments) > 1:
                return "wrapped_bd"
        elif executable in _COMMAND_RUNNERS and any(
            _runs_bd(word) for word in argv[1:]
        ):
            return "wrapped_bd"
    return None


def _literal_reason(value: str, config: JudgeConfig) -> str | None:
    """Secret and configured-sensitive refusals for one literal path word."""
    if not value or "\x00" in value:
        return None
    if _secret_path(Path(value)):
        return "secret_file_read"
    if any(part and part in value for part in config.sensitive_paths):
        return "sensitive_path"
    return None


def _label(value: str, root: Path, roots: tuple[Path, ...]) -> str:
    """An absolute path reduced to what it is relative to the repository.

    Host paths name machines and accounts. An absolute target becomes its
    repository-relative form, or the fact that it lies outside, so neither the
    request nor anything reading it learns where this checkout lives.
    """
    try:
        resolved = (root / value).resolve()
    except (OSError, RuntimeError, ValueError):
        return "[unresolved]"
    if resolved.is_relative_to(root):
        return str(resolved.relative_to(root)) or "."
    return "[allowed-root]" if any(
        resolved.is_relative_to(allowed) for allowed in roots
    ) else "[outside-roots]"


def _render(key: str, value: object, root: Path, roots: tuple[Path, ...]) -> str:
    """One argument value as text a judge can read and a payload cannot ride."""
    if key in _OPAQUE_KEYS or not isinstance(value, (str, int, float, type(None))):
        try:
            size = len(value)  # type: ignore[arg-type]
        except TypeError:
            size = 0
        return f"<{type(value).__name__}:{size}>"
    if isinstance(value, str):
        if len(value) > _VALUE_CAP:
            return f"<str:{len(value)}>"
        return _label(value, root, roots) if value.startswith("/") else value
    return repr(value)


def _screened(
    text: str, config: JudgeConfig, secrets: tuple[str, ...], *, cap: int,
) -> str:
    """Screen the whole value for secrets, then bound what survives.

    Screening precedes the cap so a secret cannot be cut in half and carried
    out in pieces, and the cap truncates rather than omits so an oversized
    argument still describes the call it belongs to.
    """
    try:
        field = sanitize_field(
            text, cap=max(len(text), 1), secret_values=secrets,
            sensitive_paths=config.sensitive_paths,
        )
    except StateError:
        return "[omitted]"
    if field.value is None:
        return "[omitted]"
    return field.value[:cap]


def inspect_tool(
    tool: ToolInput,
    repo: Path,
    *,
    allowed_roots: tuple[Path, ...] = (),
    config: JudgeConfig = JudgeConfig(),
    environ: Mapping[str, str] | None = None,
) -> ToolInspection:
    """Return an enumerated local refusal, or a bounded summary to judge.

    Only deletes of a root, home or somewhere outside the allowed roots,
    history-destroying git calls, configured sensitive paths, credential
    targets, secrets in network arguments and a `bd` call that is not the
    whole command stop here. Everything else — an unknown tool name, a
    pipeline, an argument shape this module has never seen — is summarized and
    judged.
    """
    env = os.environ if environ is None else environ
    secrets = tuple(v for k, v in env.items() if v and _SECRET_ENV.search(k))
    if (not isinstance(tool, ToolInput) or not isinstance(tool.name, str)
            or not isinstance(tool.arguments, Mapping)
            or any(not isinstance(key, str) for key in tool.arguments)):
        # Undescribable, not merely unfamiliar: there is no summary to judge.
        return _stop("unreadable_input")
    try:
        root = repo.resolve()
        roots = (root, *(path.resolve() for path in allowed_roots))
        home = Path(env.get("HOME") or Path.home()).resolve()
    except (OSError, RuntimeError, ValueError):
        return _stop("unreadable_input")

    literals: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool.arguments.get(key)
        if isinstance(value, str) and value.strip():
            literals.append(value)
            try:
                literals.append(str((root / value).resolve()))
            except (OSError, RuntimeError, ValueError):
                pass
    command = tool.arguments.get("command")
    if isinstance(command, str) and command.strip():
        for argv in _segments(command):
            argv = _unwrap(argv)
            if not argv:
                continue
            literals.extend(argv)
            executable = PurePosixPath(argv[0]).name
            if executable in {"curl", "wget"} or any(
                word.startswith(("http://", "https://")) for word in argv
            ):
                if any(secret in command for secret in secrets):
                    return _stop("secret_in_network_arguments")
            if executable == "rm":
                reason = _delete_reason(argv, root, roots, home)
                if reason is not None:
                    return _stop(reason)
            elif executable == "git":
                reason = _git_reason(argv)
                if reason is not None:
                    return _stop(reason)
    for value in literals:
        reason = _literal_reason(value, config)
        if reason is not None:
            return _stop(reason)
    if isinstance(command, str) and command.strip():
        # Last of the local refusals, so a line that is both destructive and a
        # wrapped tracker call is named by the worse of the two.
        reason = wrapped_bd_reason(command)
        if reason is not None:
            return _stop(reason)

    rendered = "; ".join(
        f"{key}={_render(key, tool.arguments[key], root, roots)}"
        for key in sorted(tool.arguments)
    )
    summary = {
        "name": _screened(tool.name, config, secrets, cap=config.title_cap),
        "arguments": _screened(rendered, config, secrets, cap=config.tool_cap),
    }
    return ToolInspection(None, json.dumps(summary, ensure_ascii=True, sort_keys=True))


def build_tool_questions(config: JudgeConfig) -> dict[str, dict[str, Any]]:
    """Reuse the reviewed atomic Noul/Score rubrics in a single request."""
    questions = build_questions(config, JudgeState(issue_id=""))
    return {key: questions[key] for key in (NEEDS_HUMAN, ACTION_RISK)}


def tool_vector(
    needs_human: float, risk: float, confidence: float,
) -> dict[ToolAction, float]:
    """The three-class vector one pre_tool answer argues for.

    The risk score says how much of the scale the answer spends arguing
    against the call. The human-need noul reports no confidence of its own, so
    how far it sits from the coin flip is its confidence — the same reading the
    pre-turn packer applies — and only its lean toward needing an operator
    claims mass. A noul of 0.5 therefore argues for nothing and leaves the
    whole vector to the risk score, which is why a hedged answer never parks a
    bead. Confidence then shrinks the result toward uniform, so a
    zero-confidence answer is the uniform vector the tie rule reads as allow:
    there is no floor to cross and no threshold to tune.
    """
    lean = max(2.0 * min(max(needs_human, 0.0), 1.0) - 1.0, 0.0)
    against = min(max(risk, 0.0), _RISK_CEILING) / _RISK_CEILING
    raw = {
        ToolAction.ALLOW: (1.0 - against) * (1.0 - lean),
        ToolAction.DENY_CALL: against * (1.0 - lean),
        ToolAction.PARK_BEAD: lean,
    }
    total = sum(raw.values())
    if total <= 0:
        raw = {entry: 1.0 for entry in _ACTIONS}
        total = float(len(_ACTIONS))
    certainty = min(max(confidence, 0.0), 1.0)
    spread = (1.0 - certainty) / len(_ACTIONS)
    return {entry: spread + certainty * (raw[entry] / total) for entry in _ACTIONS}


def route_tool(weights: Mapping[ToolAction, float]) -> ToolAction:
    """The action the vector argues for: argmax, no confidence floor."""
    return max(_ACTIONS, key=lambda entry: (weights[entry], -_ACTIONS.index(entry)))


def decide_tool(
    tool: ToolInput,
    repo: Path,
    *,
    config: JudgeConfig = JudgeConfig(),
    allowed_roots: tuple[Path, ...] = (),
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[JudgeConfig], Any] = _default_client,
) -> ToolDecision:
    """Inspect before network, then read typed answers as a three-class vector.

    Callers own opt-in and shadow observation. Neither setting can weaken a
    local denial. ALLOW means continue the existing permission flow only.
    """
    started = time.monotonic()
    environ = judge_environment(environ)
    inspection = inspect_tool(tool, repo, config=config, allowed_roots=allowed_roots,
                              environ=environ)
    if inspection.decision is not None:
        return inspection.decision
    env = os.environ if environ is None else environ

    def spent() -> float:
        return max((time.monotonic() - started) * 1000.0, 0.0)

    def failed(failure: JudgeFailure) -> ToolDecision:
        # A provider that did not answer is evidence about the service, never
        # about the bead, so a closed seat denies the call and leaves the work.
        return ToolDecision(
            ToolAction.ALLOW if config.failure_mode == FailureMode.OPEN
            else ToolAction.DENY_CALL,
            "service_failure", failure=failure, latency_ms=spent(),
        )

    if not env.get(API_KEY_ENV, "").strip():
        return failed(JudgeFailure.KEY_MISSING)

    async def ask() -> object:
        async with AsyncExitStack() as stack:
            client = client_factory(config)
            if hasattr(client, "__aenter__"):
                client = await stack.enter_async_context(client)
            elif hasattr(client, "aclose"):
                stack.push_async_callback(client.aclose)
            return await client.system_one(
                {"phase": "pre_tool", "proposed_tool": json.loads(inspection.summary)},
                build_tool_questions(config), model=config.model, timeout=config.timeout_seconds,
            )

    async def bounded() -> object:
        return await asyncio.wait_for(ask(), config.timeout_seconds)

    try:
        response = asyncio.run(bounded())
    except ImportError:
        return failed(JudgeFailure.SDK_MISSING)
    except asyncio.TimeoutError:
        return failed(JudgeFailure.TIMEOUT)
    except Exception:  # noqa: BLE001 - no provider exception text leaves this boundary
        return failed(JudgeFailure.SERVICE_ERROR)
    try:
        dump = getattr(response, "model_dump", None)
        body = _mapping(dump(mode="json") if callable(dump) else response)
        if body.get("model") != config.model:
            raise _Invalid
        answers = _mapping(body.get("answers"))
        if set(answers) != {NEEDS_HUMAN, ACTION_RISK}:
            raise _Invalid
        human = _number(_answer(answers, NEEDS_HUMAN, "noul").get("noul"), 1)
        risk_answer = _answer(answers, ACTION_RISK, "score")
        risk = _number(risk_answer.get("score"), _RISK_CEILING)
        confidence = _number(risk_answer.get("confidence"), 1)
        usage = _usage(body.get("usage"))
    except Exception:  # noqa: BLE001 - malformed answers cannot authorize tools
        return ToolDecision(ToolAction.DENY_CALL, "invalid_answer",
                            failure=JudgeFailure.INVALID_ANSWER, latency_ms=spent())
    weights = tool_vector(human, risk, confidence)
    return ToolDecision(
        route_tool(weights), "judged",
        weights[ToolAction.ALLOW], weights[ToolAction.DENY_CALL],
        weights[ToolAction.PARK_BEAD], latency_ms=spent(), usage=usage,
    )
