"""Supplemental tool policy. An allow result still requires existing permissions.

Inspect literal targets before constructing a provider client. This module never
executes shell text, reads target contents, or replaces the execution sandbox.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from ortus.core.judge import FailureMode, JudgeConfig, JudgeState
from ortus.core.judge_state import StateError, sanitize_field
from ortus.core.judge_typesafe import (
    ACTION_RISK, API_KEY_ENV, NEEDS_HUMAN, _answer, _default_client,
    _Invalid, _mapping, _number, build_questions,
)

MAX_INPUT_BYTES = 65536
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)
_SECRET_FILES = {"credentials", "credentials.json", "id_rsa", "id_ed25519", "auth.json"}
_SECRET_DIRS = {".ssh", ".aws", ".gnupg"}


class ToolAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    HUMAN = "human"


@dataclass(frozen=True)
class ToolDecision:
    action: ToolAction
    reason: str


@dataclass(frozen=True)
class ToolInput:
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolInspection:
    decision: ToolDecision | None
    # Only locally screened fields, never the original arguments.
    summary: str = ""


def _stop(action: ToolAction, reason: str) -> ToolInspection:
    return ToolInspection(ToolDecision(action, reason))


def _secret_path(path: Path) -> bool:
    return any(
        part in _SECRET_DIRS or part in _SECRET_FILES
        or part == ".env" or part.startswith(".env.")
        or part.endswith((".pem", ".key"))
        for part in path.parts
    )


def inspect_tool(
    tool: ToolInput,
    repo: Path,
    *,
    allowed_roots: tuple[Path, ...] = (),
    config: JudgeConfig = JudgeConfig(),
    environ: Mapping[str, str] | None = None,
) -> ToolInspection:
    """Return a local refusal or a bounded summary eligible for soft judgment.

    Unknown tools and shell grammars require a human. Allowed roots authorize
    literal file targets only; they never exempt secret reads or root deletion.
    """
    env = os.environ if environ is None else environ
    secrets = tuple(v for k, v in env.items() if v and _SECRET_ENV.search(k))
    human = ToolAction.HUMAN
    deny = ToolAction.DENY
    if not isinstance(tool, ToolInput) or not isinstance(tool.name, str):
        return _stop(human, "unsupported_input")
    if not isinstance(tool.arguments, Mapping):
        return _stop(human, "unsupported_input")
    try:
        raw = json.dumps(dict(tool.arguments), allow_nan=False, ensure_ascii=False)
        if len(raw.encode("utf-8")) + len(tool.name.encode("utf-8")) > MAX_INPUT_BYTES:
            return _stop(human, "oversized_input")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return _stop(human, "unsupported_input")
    if any(not isinstance(key, str) for key in tool.arguments):
        return _stop(human, "unsupported_input")
    try:
        root = repo.resolve()
        roots = (root, *(path.resolve() for path in allowed_roots))
        safe_name = sanitize_field(tool.name, cap=160, secret_values=secrets)
    except (OSError, RuntimeError, ValueError, StateError):
        return _stop(human, "unsupported_input")
    if not safe_name.value:
        return _stop(human, "sensitive_or_oversized_name")
    args = tool.arguments
    targets: list[tuple[str, bool]] = []
    summary: dict[str, object] = {"name": safe_name.value}
    if tool.name in {"Read", "Write", "Edit", "Glob", "Grep"}:
        key = "path" if tool.name in {"Glob", "Grep"} else "file_path"
        value = args.get(key, "." if key == "path" else None)
        if not isinstance(value, str) or not value.strip():
            return _stop(human, "unsupported_input")
        targets.append((value, tool.name != "Write"))
    elif tool.name == "Bash":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _stop(human, "unsupported_input")
        # Conservative even inside quotes. No expansion or evaluation is safe here.
        if any(char in command for char in "|&;<>`$\n\r(){}*?[]~\\"):
            return _stop(human, "opaque_shell")
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            return _stop(human, "opaque_shell")
        if not argv:
            return _stop(human, "unsupported_input")
        executable = argv[0]
        # Paths to executables, wrappers and nested interpreters are not resolved.
        if executable not in {"rm", "cat", "head", "tail", "touch", "mkdir", "cp", "mv", "pwd", "curl", "wget"}:
            return _stop(human, "unsupported_command")
        summary["command"] = executable
        if executable in {"curl", "wget"}:
            if any(secret in command for secret in secrets):
                return _stop(deny, "secret_in_network_arguments")
            # Uploads, output paths and option grammars need operator inspection.
            if len(argv) != 2 or not argv[1].startswith(("https://", "http://")):
                return _stop(human, "unsupported_network_arguments")
            if sanitize_field(argv[1], cap=config.tool_cap, secret_values=secrets).value is None:
                return _stop(human, "sensitive_network_arguments")
            summary["network"] = True
        else:
            operands: list[str] = []
            recursive = False
            options = True
            for word in argv[1:]:
                if options and word == "--":
                    options = False
                elif options and word.startswith("-"):
                    if executable == "rm" and (
                        word in {"--recursive", "--force", "--dir", "--verbose", "--no-preserve-root"}
                        or re.fullmatch(r"-[fFrRdivI]+", word)
                    ):
                        recursive |= word == "--recursive" or (
                            not word.startswith("--") and ("r" in word or "R" in word)
                        )
                    else:
                        return _stop(human, "unsupported_options")
                else:
                    operands.append(word)
            if executable == "pwd":
                if operands:
                    return _stop(human, "unsupported_input")
            elif not operands:
                return _stop(human, "unknown_target")
            for operand in operands:
                try:
                    if recursive and (root / operand).resolve() == Path("/"):
                        return _stop(deny, "recursive_root_deletion")
                except (OSError, RuntimeError, ValueError):
                    return _stop(human, "unresolved_path")
                targets.append((operand, executable in {"cat", "head", "tail", "cp", "mv"}))
    else:
        return _stop(human, "unsupported_tool")

    # A supplied working directory would change all relative shell operands.
    if tool.name == "Bash" and "cwd" in args:
        return _stop(human, "unsupported_working_directory")
    safe_targets = []
    for value, reading in targets:
        if "\x00" in value or value.startswith("~") or "$" in value:
            return _stop(human, "unresolved_path")
        try:
            lexical = root / value
            resolved = lexical.resolve()
        except (OSError, RuntimeError, ValueError):
            return _stop(human, "unresolved_path")
        if reading and (_secret_path(lexical) or _secret_path(resolved)):
            return _stop(deny, "secret_file_read")
        if not any(resolved.is_relative_to(allowed) for allowed in roots):
            return _stop(deny, "path_escape")
        if any(part and part in str(resolved) for part in config.sensitive_paths):
            return _stop(deny if reading else human, "sensitive_path")
        # Do not disclose absolute host paths, including configured outside roots.
        label = str(resolved.relative_to(root)) if resolved.is_relative_to(root) else "[allowed-root]"
        try:
            safe = sanitize_field(label, cap=config.tool_cap, secret_values=secrets,
                                  sensitive_paths=config.sensitive_paths)
        except StateError:
            return _stop(human, "unsupported_input")
        safe_targets.append(safe.value or "[omitted]")
    if tool.name in {"Glob", "Grep"}:
        # Search patterns and recursive symlink traversal are not literal targets.
        # Their explicit base path still receives the hard checks above.
        return _stop(human, "unsupported_search_targets")
    if safe_targets:
        summary["paths"] = safe_targets
    packed = json.dumps(summary, ensure_ascii=True, sort_keys=True)
    if len(packed.encode()) > min(config.total_bytes_cap, config.tool_cap):
        return _stop(human, "oversized_summary")
    return ToolInspection(None, packed)


def build_tool_questions(config: JudgeConfig) -> dict[str, dict[str, Any]]:
    """Reuse the reviewed atomic Noul/Score rubrics in a single request."""
    questions = build_questions(config, JudgeState(issue_id=""))
    return {key: questions[key] for key in (NEEDS_HUMAN, ACTION_RISK)}


def decide_tool(
    tool: ToolInput,
    repo: Path,
    *,
    config: JudgeConfig = JudgeConfig(),
    allowed_roots: tuple[Path, ...] = (),
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[JudgeConfig], Any] = _default_client,
) -> ToolDecision:
    """Inspect before network, then apply fixed tool thresholds to typed answers.

    Callers own opt-in and shadow observation. Neither setting can weaken a
    local denial. ALLOW means continue the existing permission flow only.
    """
    inspection = inspect_tool(tool, repo, config=config, allowed_roots=allowed_roots,
                              environ=environ)
    if inspection.decision is not None:
        return inspection.decision
    env = os.environ if environ is None else environ
    failure = ToolDecision(
        ToolAction.ALLOW if config.failure_mode == FailureMode.OPEN else ToolAction.HUMAN,
        "service_failure",
    )
    if not env.get(API_KEY_ENV, "").strip():
        return failure

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
    except Exception:  # noqa: BLE001 - no provider exception text leaves this boundary
        return failure
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
        risk = _number(risk_answer.get("score"), 2)
        confidence = _number(risk_answer.get("confidence"), 1)
    except Exception:  # noqa: BLE001 - malformed answers cannot authorize tools
        return ToolDecision(ToolAction.HUMAN, "invalid_answer")
    if max(human, 1 - human) < 0.8 or confidence < 0.8:
        return ToolDecision(ToolAction.HUMAN, "low_confidence")
    if human >= 0.8 or risk >= 1.5:
        return ToolDecision(ToolAction.HUMAN, "needs_human")
    return ToolDecision(ToolAction.ALLOW, "judged_safe")
