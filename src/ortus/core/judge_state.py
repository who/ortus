"""Prepare bounded judge input from an in-memory issue, without file access.

Issue prose remains untrusted data. Inclusion requires operator review and an
explicit opt-in; pattern matching cannot establish that arbitrary prose is safe.
Omission diagnostics contain only fixed field names and reason enums.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Mapping

from ortus.core.judge import (
    WORKER_ROUTES,
    JudgeConfig, JudgePhase, JudgeRoute, JudgeState, ProposedTool,
)


class StateError(ValueError):
    """Invalid input or a budget too small for the empty state, without raw data."""


class OmissionReason(str, Enum):
    SENSITIVE = "sensitive"
    OVERSIZE = "oversize"
    TEXT_DISABLED = "text_disabled"
    PRIVATE = "private"
    TOTAL_BUDGET = "total_budget"


@dataclass(frozen=True)
class SanitizedField:
    value: str | None
    reason: OmissionReason | None = None


@dataclass(frozen=True)
class Omission:
    field: str
    reason: OmissionReason


def _json(state: JudgeState) -> str:
    payload = asdict(state)
    if state.phase != JudgePhase.SEMANTIC_READINESS:
        payload.pop("design")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PackedState:
    """Prepared typed input plus local diagnostics, never sent as issue prose.

    ``to_json`` defines the UTF-8 budgeted transport representation. Empty text
    and metadata denote omitted fields; no raw issue or environment is retained.
    """

    state: JudgeState
    omissions: tuple[Omission, ...]

    def to_json(self) -> str:
        return _json(self.state)

    def to_payload(self) -> dict[str, object]:
        return json.loads(self.to_json())


_SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
_SENSITIVE = re.compile(
    r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+"
    r"|-----BEGIN [^-\r\n]*PRIVATE KEY-----"
    r"|\b(?:[\w-]*(?:api[_-]?key|token|secret|password|passwd|credential)[\w-]*)"
    r"[\s\"']*[:=]"
    r"|\b(?:authorization\s*[:=]|bearer\s+|basic\s+)"
    r"|\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}"
    r"|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16}"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
    r"|(?:^|[/\\\s])(?:\.env(?:\.[\w.-]+)?|\.ssh|\.aws|\.gnupg)(?:$|[/\\\s])",
    re.I,
)


def environment_secrets(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Literal values of credential-named environment variables, for matching.

    The names are matched, never recorded, and the values are only ever handed
    to :func:`sanitize_field` as strings to look for. A caller that screens
    text it read from disk needs the same list the packer builds for issue
    prose, so it lives here beside the pattern that defines "credential-named".
    """
    env = os.environ if environ is None else environ
    return tuple(
        value for name, value in env.items() if value and _SECRET_NAME.search(name)
    )


def sanitize_field(
    value: str,
    *,
    cap: int,
    secret_values: tuple[str, ...] = (),
    sensitive_paths: tuple[str, ...] = (),
) -> SanitizedField:
    """Inspect the entire string before applying a character cap; never truncate."""
    if not isinstance(value, str) or type(cap) is not int or cap < 0:
        raise StateError("invalid field or cap")
    if any(not isinstance(item, str) for item in (*secret_values, *sensitive_paths)):
        raise StateError("invalid sanitizer configuration")
    if _SENSITIVE.search(value) or any(
        literal and literal in value for literal in (*secret_values, *sensitive_paths)
    ):
        return SanitizedField(None, OmissionReason.SENSITIVE)
    # Surrogates cannot be serialized as UTF-8. Do not echo encoding errors.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise StateError("invalid text encoding")
    if len(value) > cap:
        return SanitizedField(None, OmissionReason.OVERSIZE)
    return SanitizedField(value)


def _section(text: str, heading: str) -> str:
    lines: list[str] = []
    active = False
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            if active:
                break
            active = match.group(1).casefold() == heading.casefold()
        elif active:
            lines.append(line)
    return "\n".join(lines).strip()


def pack_state(
    issue: Mapping[str, object],
    config: JudgeConfig,
    *,
    backends_available: tuple[JudgeRoute, ...] | None = None,
    phase: JudgePhase = JudgePhase.PRE_TURN,
    proposed_tool: ProposedTool | None = None,
    environ: Mapping[str, str] | None = None,
) -> PackedState:
    """Allowlist packet fields, omit unsafe values, then fit the serialized state.

    Environment values with credential-like names are used only for matching.
    The caller can supply an environment snapshot for a fully pure operation.
    No logs, attachments, case evidence, paths or transcript files are opened.
    Metadata uses the title cap; tool names and summaries use the tool cap.
    Full source fields are screened before Objective/AC first-line extraction.
    """
    if not isinstance(issue, Mapping):
        raise StateError("invalid issue packet")
    try:
        json.dumps(dict(issue), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise StateError("issue packet is not JSON serializable") from None
    for field in ("id", "issue_type", "type", "title", "description", "acceptance_criteria"):
        if field in issue and not isinstance(issue[field], str):
            raise StateError("invalid issue field type")
    labels = issue.get("labels", ())
    if not isinstance(labels, (list, tuple)) or any(not isinstance(x, str) for x in labels):
        raise StateError("invalid issue labels")
    priority = issue.get("priority")
    if priority is not None and (type(priority) is not int or not 0 <= priority <= 4):
        raise StateError("invalid issue priority")
    if not isinstance(phase, JudgePhase):
        raise StateError("invalid judge phase")
    if proposed_tool is not None and not isinstance(proposed_tool, ProposedTool):
        raise StateError("invalid proposed tool")
    env = os.environ if environ is None else environ
    if not isinstance(env, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        raise StateError("invalid environment")
    secrets = tuple(v for k, v in env.items() if v and _SECRET_NAME.search(k))
    omissions: list[Omission] = []

    def clean(field: str, value: str, cap: int) -> str:
        result = sanitize_field(
            value, cap=cap, secret_values=secrets, sensitive_paths=config.sensitive_paths,
        )
        if result.reason is not None:
            item = Omission(field, result.reason)
            if item not in omissions:
                omissions.append(item)
        return result.value or ""

    routes = backends_available if backends_available is not None else tuple(
        route for route in config.routes if route in WORKER_ROUTES
    )
    if not isinstance(routes, (list, tuple)) or any(
        not isinstance(route, JudgeRoute)
        or route not in WORKER_ROUTES
        or route not in config.routes for route in routes
    ):
        raise StateError("invalid available backends")
    safe_labels = tuple(sorted({clean("labels", label, config.title_cap) for label in labels} - {""}))
    state = JudgeState(
        issue_id=clean("issue_id", issue.get("id", ""), config.title_cap),
        seat=clean("seat", config.seat, config.title_cap),
        phase=phase,
        issue_type=clean("issue_type", issue.get("issue_type", issue.get("type", "task")), config.title_cap),
        labels=safe_labels,
        priority=priority,
        backends_available=tuple(sorted(set(routes), key=lambda route: route.value)),
    )
    if config.allows_issue_text(tuple(labels)):
        title = clean("title", issue.get("title", ""), config.title_cap)
        # Scan and cap the source before extracting a safe first line. A secret
        # on a later line must suppress the whole field, not disappear in slicing.
        description = clean("objective", issue.get("description", ""), config.objective_cap)
        acceptance = clean("acceptance", issue.get("acceptance_criteria", ""), config.acceptance_cap)
        objective = _section(description, "Objective")
        criteria = _section(acceptance, "Observable criteria") or acceptance
        first_lines = [line for line in criteria.splitlines() if re.match(r"^\s*(?:[-*]\s*)?AC-\d+\b", line)]
        state = replace(
            state, title=title,
            objective=next((line.strip() for line in objective.splitlines() if line.strip()), ""),
            acceptance="\n".join(first_lines),
        )
        if phase == JudgePhase.SEMANTIC_READINESS:
            state = replace(
                state, objective=objective, acceptance=acceptance,
                design=clean("design", issue.get("design", ""), config.objective_cap),
            )
    else:
        reason = OmissionReason.PRIVATE if "judge-private" in labels else OmissionReason.TEXT_DISABLED
        omissions.append(Omission("issue_text", reason))
    if proposed_tool is not None:
        name = clean("tool_name", proposed_tool.name, config.tool_cap)
        summary = clean("tool_summary", proposed_tool.arg_summary, config.tool_cap)
        if name:
            state = replace(state, proposed_tool=ProposedTool(name, summary))

    from ortus.core.judge_packs import CRITERIA_VERSION, criteria_hash
    from ortus.core.judge_typesafe import build_questions

    state = replace(
        state, criteria_version=CRITERIA_VERSION,
        criteria_hash=criteria_hash(config, build_questions(config, state)),
    )

    # Keep routing metadata longest. Each reduction drops a whole field and the
    # final check includes JSON escaping, keys, delimiters and multibyte text.
    for field, empty in (
        ("proposed_tool", None), ("design", ""), ("acceptance", ""), ("objective", ""),
        ("title", ""), ("labels", ()), ("issue_type", ""), ("issue_id", ""),
        ("seat", ""), ("priority", None), ("backends_available", ()),
    ):
        if len(_json(state).encode("utf-8")) <= config.total_bytes_cap:
            break
        if getattr(state, field) != empty:
            state = replace(state, **{field: empty})
            omissions.append(Omission(field, OmissionReason.TOTAL_BUDGET))
    if len(_json(state).encode("utf-8")) > config.total_bytes_cap:
        raise StateError("judge state budget is too small")
    return PackedState(state, tuple(omissions))
