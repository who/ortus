"""Private Claude PreToolUse command adapter, invoked with ``python -m``.

The parent supplies ORTUS_JUDGE_HOOK_CONTEXT, an absolute path to a private
JSON file containing version=1, repo, issue_id, session_id, judge and optional
allowed_roots. Its directory is the run's signal inbox. The parent must keep
this snapshot private and arrange sandbox access. This is supplementary
enforcement inside the worker trust boundary, not tamper-proof isolation.
No project config or hook-input field supplies trusted policy. Registration
and consumption of human-*.json signals belong to the parent launcher.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import sys
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Mapping

from ortus.core.config import Config
from ortus.core.judge import JudgeConfig, JudgeMode, parse_judge_config
from ortus.core.judge_log import question_version
from ortus.core.judge_state import environment_secrets
from ortus.core.judge_tools import (
    MAX_INPUT_BYTES, ToolAction, ToolDecision, ToolInput, build_tool_questions,
    decide_tool, inspect_tool, redacted_command,
)
from ortus.core.output import progress

CONTEXT_ENV = "ORTUS_JUDGE_HOOK_CONTEXT"
WATCHDOG_SECONDS = 5.0

#: Longest tool name recorded. The parent screens it again before it reaches
#: the log; this only keeps one record inside the reader's line budget.
TOOL_NAME_CAP = 160


class HookReason(str, Enum):
    POLICY_DENIED = "policy_denied"
    DENIED_CALL = "denied_call"
    NEEDS_HUMAN = "needs_human"
    INVALID_INPUT = "invalid_input"
    INVALID_CONTEXT = "invalid_context"
    INTERNAL_FAILURE = "internal_failure"
    WATCHDOG = "watchdog"
    SIGNAL_FAILURE = "signal_failure"


class InvalidInput(ValueError):
    """Hook input this adapter cannot read, named by a bounded subreason."""

    def __init__(self, reason: str = "malformed_json") -> None:
        super().__init__(reason)
        self.reason = reason

    def code(self) -> str:
        """The failure code a fail-open record carries for this input."""
        return "oversize_input" if self.reason == "oversize" else f"invalid_input:{self.reason}"


class WatchdogExpired(BaseException):
    """Bypass provider exception handling, so a fired watchdog is named as one."""


@dataclass(frozen=True)
class HookContext:
    repo: Path
    issue_id: str
    session_id: str
    directory: Path
    config: JudgeConfig
    allowed_roots: tuple[Path, ...]
    run_id: str = ""
    #: The bead's Scope and Non-goals, passed to pre_tool as trusted spec text.
    scope: Mapping[str, str] | None = None


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise InvalidInput


def read_object(stream: BinaryIO) -> dict:
    """Read one bounded object, including one extra byte to detect overflow."""
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise InvalidInput("oversize")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidInput from None
    if not isinstance(body, dict):
        raise InvalidInput
    return body


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InvalidInput("missing_field")
    return value


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InvalidInput


def load_context(environ: Mapping[str, str]) -> HookContext:
    path = Path(environ[CONTEXT_ENV])
    if not path.is_absolute() or path.resolve() != path:
        raise InvalidInput
    _private(path.parent.stat(), directory=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        _private(os.fstat(stream.fileno()))
        body = read_object(stream)
    if type(body.get("version")) is not int or body["version"] != 1:
        raise InvalidInput
    if set(body) - {"version", "repo", "issue_id", "session_id", "judge", "allowed_roots",
                    "run_id", "scope"}:
        raise InvalidInput
    repo = Path(_text(body, "repo"))
    if not repo.is_absolute() or not repo.is_dir() or repo.resolve() != repo:
        raise InvalidInput
    issue = _text(body, "issue_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", issue):
        raise InvalidInput
    roots = body.get("allowed_roots", [])
    if not isinstance(roots, list) or any(
        not isinstance(root, str) or not Path(root).is_absolute() for root in roots
    ):
        raise InvalidInput
    if not isinstance(body.get("judge"), dict):
        raise InvalidInput
    config = parse_judge_config(Config(values={"judge": body["judge"]}), environ={})
    run_id = _text(body, "run_id") if "run_id" in body else ""
    scope = body.get("scope")
    if scope is not None and (not isinstance(scope, dict) or set(scope) - {"scope", "non_goals"}
                              or any(not isinstance(v, str) for v in scope.values())):
        raise InvalidInput
    return HookContext(repo, issue, _text(body, "session_id"), path.parent,
                       config, tuple(Path(root).resolve() for root in roots), run_id,
                       scope)


def parse_input(body: dict, context: HookContext) -> tuple[ToolInput, Path]:
    """The call and the resolved directory it runs in.

    Claude's Bash tool keeps a `cd`, so a worker that once changed into a
    subdirectory sends every later call from there. Any directory inside the
    repository is accepted and returned, and relative operands are inspected
    against it rather than against the root.
    """
    if body.get("hook_event_name") != "PreToolUse":
        raise InvalidInput("hook_event")
    if _text(body, "session_id") != context.session_id:
        raise InvalidInput("session_mismatch")
    cwd = Path(_text(body, "cwd"))
    if not cwd.is_absolute():
        raise InvalidInput("cwd_outside_repo")
    try:
        cwd = cwd.resolve(strict=True)
    except (OSError, RuntimeError):
        raise InvalidInput("cwd_unresolvable") from None
    if not cwd.is_relative_to(context.repo):
        raise InvalidInput("cwd_outside_repo")
    _text(body, "tool_use_id")
    name = _text(body, "tool_name")
    if not isinstance(body.get("tool_input"), dict):
        raise InvalidInput("tool_input_type")
    return ToolInput(name, body["tool_input"]), cwd


def _publish(context: HookContext, prefix: str, body: dict[str, object]) -> None:
    """Publish one durable complete record into the run's private inbox."""
    call_id = uuid.uuid4().hex
    record = {"version": 1, "call_id": call_id, "issue_id": context.issue_id,
              "session_hash": hashlib.sha256(context.session_id.encode()).hexdigest(),
              **body}
    if context.run_id:
        record["run_id"] = context.run_id
    temporary = context.directory / f".{prefix}-{call_id}.tmp"
    destination = context.directory / f"{prefix}-{call_id}.json"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(context.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_human_signal(context: HookContext) -> None:
    """Publish a park request; never persist tool arguments or text."""
    _publish(context, "human", {"reason": HookReason.NEEDS_HUMAN.value})


#: The pre_tool question set's name in a record's `question_version`.
TOOL_QUESTIONS = "tool-v2"


def write_tool_record(
    context: HookContext, tool: str, decision: ToolDecision, applied: ToolAction,
    *, call: ToolInput | None = None, local: bool = False,
) -> None:
    """Publish what this call was decided as, for the parent to log.

    The hook cannot append to the repository's decision log — the worker owns
    that tree — so it records the outcome in the same inbox the parent already
    reads. Every decision is recorded, including the allowed ones and the ones
    local policy answered with no request. With ``call`` the record also
    carries :func:`redacted_command` — the command with secrets, sensitive
    paths and heredoc bodies replaced, capped at `tool_cap` — and the
    comparison fields: the baseline lets every call local policy passes, and
    Jev's action is the argmax of its vector when it has one.
    """
    usage = decision.usage
    extra: dict[str, object] = {}
    if call is not None:
        vector = decision.vector()
        extra = {
            "command": redacted_command(
                call, context.config, (context.repo, *context.allowed_roots),
                environment_secrets(),
            ),
            "baseline_action": (decision.action.value if local
                                else ToolAction.ALLOW.value),
            "jev_action": decision.action.value if vector is not None else None,
            "question_version": None if local else question_version(
                TOOL_QUESTIONS, build_tool_questions(context.config),
            ),
            "option_order": None,
        }
        if decision.narrow is not None:
            extra["narrow_answers"] = dict(decision.narrow)
    _publish(context, "tool", {
        "reason": decision.reason,
        "tool": tool[:TOOL_NAME_CAP],
        "action": decision.action.value,
        "effective_action": applied.value,
        "vector": decision.vector(),
        "failure": decision.failure.value if decision.failure is not None else None,
        "latency_ms": round(decision.latency_ms, 3),
        "input_tokens": usage.input_tokens if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        **extra,
    })


def write_failure_record(context: HookContext, tool: str, failure: str) -> None:
    """Publish a hook-side failure: no decision was reached and the call went on.

    The record keeps the tool-decision shape so the parent logs it like any
    other call, with `hook_failure` naming what broke.
    """
    _publish(context, "tool", {
        "reason": failure,
        "tool": tool[:TOOL_NAME_CAP] or "unknown",
        "action": ToolAction.ALLOW.value,
        "effective_action": ToolAction.ALLOW.value,
        "vector": None,
        "failure": None,
        "latency_ms": 0.0,
        "input_tokens": None,
        "output_tokens": None,
        "hook_failure": failure,
    })


def _fail_open(failure: str, context: HookContext | None = None, tool: str = "") -> int:
    """End in the normal permission flow: exit 0 and no permission decision.

    A broken hook is not a decision, so it never blocks a call. The failure is
    recorded when the context loaded, and named on stderr either way.
    """
    signal.setitimer(signal.ITIMER_REAL, 0)
    if context is not None:
        try:
            write_failure_record(context, tool, failure)
        except Exception:  # noqa: BLE001 - a lost record still allows the call
            pass
    progress("judge-hook", f"done (fail open: {failure})")
    return 0


def _deny(reason: HookReason, code: int = 0) -> int:
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
               "permissionDecision": "deny", "permissionDecisionReason": reason.value}},
              sys.stdout)
    sys.stdout.write("\n")
    progress("judge-hook", f"done ({reason.value})")
    return code


def _expire(signum: int, frame: object) -> None:
    raise WatchdogExpired


def main() -> int:
    """No tracker writes, prompts, raw exceptions or explicit permission grants.

    Only a decision blocks a call: a local-policy refusal or a judgment, and
    only in enforce. Every hook-side failure — unreadable input, a missing
    context, a provider that did not answer, the watchdog, an internal error —
    fails open through :func:`_fail_open`, in every mode.
    """
    previous = signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, WATCHDOG_SECONDS)
    context: HookContext | None = None
    name = ""
    try:
        progress("judge-hook", "reading tool hook input")
        body: dict | None = None
        failure = ""
        try:
            body = read_object(sys.stdin.buffer)
        except InvalidInput as error:
            failure = error.code()
        try:
            context = load_context(os.environ)
        except Exception:
            return _fail_open("invalid_context")
        if body is None:
            return _fail_open(failure, context)
        if isinstance(body.get("tool_name"), str):
            name = body["tool_name"]
        try:
            tool, cwd = parse_input(body, context)
        except InvalidInput as error:
            return _fail_open(error.code(), context, name)
        progress("judge-hook", "checking tool policy")
        # Provider libraries must not contaminate protocol output or leak payloads.
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            inspection = inspect_tool(tool, context.repo, config=context.config,
                                      allowed_roots=context.allowed_roots, cwd=cwd)
            decision = inspection.decision or decide_tool(
                tool, context.repo, config=context.config, allowed_roots=context.allowed_roots,
                scope=context.scope, cwd=cwd,
            )
        local = inspection.decision is not None
        if not local and decision.failure is not None:
            # A provider that did not answer decided nothing about this call.
            decision = replace(decision, action=ToolAction.ALLOW)
        # Shadow never changes worker behavior: local policy and judgments are
        # both recorded and allowed, so a collect arm runs like a hook-less one.
        shadow = context.config.mode == JudgeMode.SHADOW
        applied = ToolAction.ALLOW if shadow else decision.action
        if applied not in set(ToolAction):
            return _fail_open("internal", context, name)
        try:
            write_tool_record(context, tool.name, decision, applied,
                              call=tool, local=local)
        except Exception:  # noqa: BLE001 - a record is evidence, not a verdict
            # A record that cannot be published is a logging fault. It must not
            # turn an allowed call into a refusal or a refusal into a pass.
            pass
        if applied == ToolAction.DENY_CALL:
            # Only this call is refused. The worker keeps its claim, its
            # window and its next attempt; a park is the other action.
            return _deny(HookReason.POLICY_DENIED if local else HookReason.DENIED_CALL)
        if applied == ToolAction.PARK_BEAD:
            try:
                write_human_signal(context)
            except Exception:
                return _deny(HookReason.SIGNAL_FAILURE, 2)
            return _deny(HookReason.NEEDS_HUMAN)
        progress("judge-hook", "done (normal permission flow)")
        return 0
    except WatchdogExpired:
        return _fail_open("watchdog", context, name)
    except Exception:
        return _fail_open("internal", context, name)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
