"""Private, bounded gate events for replay and operational diagnosis.

Call write_decision before launching a worker. JudgeLogError must propagate to
the enforced gate caller, leaving its owned claim available for recovery.
These functions never change tracker state or apply service fail-open policy.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping
from uuid import UUID, uuid4

from ortus.core import output
from ortus.core.judge import (
    WORKER_ROUTES,
    GateAction, GateDecision, GateReason, JudgeAnswers, JudgeConfig, JudgeMode, JudgePhase,
)
from ortus.core.judge import JudgeRoute
from ortus.core.judge_state import (
    TRUNCATABLE_FIELDS, StateError, environment_secrets, sanitize_field,
)
from ortus.core.judge_tools import ToolAction, _screened, cap_command
from ortus.core.judge_typesafe import JudgeFailure, JudgeUsage

MAX_EVENT_BYTES = 8192
LOG_NAME = "jev-decisions.jsonl"

#: The pre_tool outcome vocabulary, taken from the enum that produces it so a
#: new action cannot be written here without the code that decided it.
TOOL_ACTIONS = frozenset(action.value for action in ToolAction)

#: Per-bead model-routing records. A separate file rather than a fourth event
#: kind in LOG_NAME: the replay reader rejects any record whose event name it
#: does not know, so mixing these in would invalidate the decision log it
#: already validates. `run_id` and `decision_id` join a routing record back to
#: the gate decision and its outcome.
ROUTE_LOG_NAME = "jev-model-routes.jsonl"

#: Longest tool command a `tool_decision` record carries, whatever `tool_cap`
#: says. Four bytes a character at worst keeps the record well inside
#: MAX_EVENT_BYTES beside every other field.
MAX_COMMAND_CHARS = 1024

#: `<question set>-v<n>:<first 16 hex of the resolved-question hash>`. The
#: name says which wording a decision answered and the hash which resolution
#: of it, so a reworded question never scores against the old one's labels.
QUESTION_VERSION = re.compile(r"[a-z][a-z0-9-]{0,47}-v\d+:[a-f0-9]{16}")


class LogFailure(str, Enum):
    INVALID_EVENT = "invalid_event"
    UNSAFE_PATH = "unsafe_path"
    INCOMPLETE_LOG = "incomplete_log"
    IO_ERROR = "io_error"


class JudgeLogError(RuntimeError):
    """Safe to display: no paths, provider errors or event values."""

    def __init__(self, failure: LogFailure):
        self.failure = failure
        super().__init__(f"judge log failed: {failure.value}")


class OutcomeStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DecisionEvent:
    run_id: UUID
    seat: str
    issue_id: str
    phase: JudgePhase
    answers: JudgeAnswers | None
    decision: GateDecision
    model: str
    criteria_version: str
    criteria_hash: str
    latency_ms: float
    failure: JudgeFailure | None = None
    usage: JudgeUsage | None = None
    truncated_fields: tuple[str, ...] = ()
    decision_id: UUID = field(default_factory=uuid4)
    #: The route options in the order the request presented them.
    option_order: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeEvent:
    run_id: UUID
    decision_id: UUID
    observed_status: OutcomeStatus
    elapsed_worker_ms: float
    usage: JudgeUsage | None = None


@dataclass(frozen=True)
class ModelRouteEvent:
    """What the vectors were and which model the bead got for them.

    ``model`` and ``reasoning_effort`` are the values the route chose, including
    a provider default recorded as ``None``. The three vector fields are
    ``None`` when no answers reached the router, so a flag-off or judge-failure
    row never reads as a scored bottom of the scale.

    ``applied`` separates a route the worker ran on from one only observed. A
    shadow seat records what it would have picked while its worker keeps the
    pinned profile, and a reader joining tiers to bead outcomes has to be able
    to drop the rows whose tier never ran the bead.
    """

    run_id: UUID
    decision_id: UUID
    seat: str
    issue_id: str
    backend: str
    tier: str
    reason: str
    model: str | None = None
    reasoning_effort: str | None = None
    needs_frontier: float | None = None
    action_risk: float | None = None
    difficulty: float | None = None
    applied: bool = True
    #: The tier the vectors argue for, before a profile that cannot be mapped
    #: sends the bead back to baseline; None when no answers reached the router.
    jev_tier: str | None = None
    #: The router question set the vectors answered; None for legacy answers.
    question_version: str | None = None


def elapsed_ms(started_at: float) -> float:
    """Measure from a time.monotonic() sample, never from wall-clock stamps."""
    start = _number(started_at)
    return _number((time.monotonic() - start) * 1000)


def _invalid() -> None:
    raise JudgeLogError(LogFailure.INVALID_EVENT)


def _number(value: object, maximum: float | None = None) -> float:
    if type(value) not in (int, float):
        _invalid()
    try:
        number = float(value)
    except OverflowError:
        raise JudgeLogError(LogFailure.INVALID_EVENT) from None
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        _invalid()
    return number


def _enum(value: object, kind: type[Enum]) -> str:
    if not isinstance(value, kind):
        _invalid()
    return value.value


def _common(kind: str, run_id: UUID, decision_id: UUID) -> dict:
    if type(run_id) is not UUID or type(decision_id) is not UUID:
        _invalid()
    return {
        "schema_version": 1,
        "event": kind,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": str(run_id),
        "decision_id": str(decision_id),
    }


def _usage(usage: JudgeUsage | None) -> dict:
    if usage is not None and (
        not isinstance(usage, JudgeUsage)
        or any(type(n) is not int or not 0 <= n <= 2**63 - 1
               for n in (usage.input_tokens, usage.output_tokens))
    ):
        _invalid()
    # No explicit pricing is recorded by this API, so a dollar cost is unknown.
    return {
        "input_tokens": usage.input_tokens if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        "measured_cost_usd": None,
    }


def question_version(name: str, questions: Mapping[str, object]) -> str:
    """The version string one site's resolved questions log under."""
    digest = hashlib.sha256(json.dumps(
        questions, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    version = f"{name}:{digest[:16]}"
    if not QUESTION_VERSION.fullmatch(version):
        _invalid()
    return version


def side_fields(
    vocabulary: frozenset[str] | set[str], *,
    baseline_action: str | None, jev_action: str | None,
    question_version: str | None = None, option_order: object = None,
    options: frozenset[str] | set[str] | None = None,
) -> dict:
    """The comparison fields every Jev record kind may carry beside its own.

    `baseline_action` is what plain code did or would have done and
    `jev_action` what the site's own argmax reads off Jev's answer, both drawn
    from the site's action vocabulary so neither can carry prose. Either is
    null where it has no meaning: no answer, or no baseline. `option_order`
    lists the choice options in the order they were presented, drawn from
    `options` when the question's choices are not the site's actions.
    """
    for action in (baseline_action, jev_action):
        if action is not None and action not in vocabulary:
            _invalid()
    if question_version is not None and (
        not isinstance(question_version, str)
        or not QUESTION_VERSION.fullmatch(question_version)
    ):
        _invalid()
    if option_order is not None:
        known = vocabulary if options is None else options
        if not isinstance(option_order, (list, tuple)) or any(
            not isinstance(name, str) or name not in known for name in option_order
        ) or len(set(option_order)) != len(option_order):
            _invalid()
        option_order = list(option_order)
    return {
        "baseline_action": baseline_action,
        "jev_action": jev_action,
        "question_version": question_version,
        "option_order": option_order,
    }


#: Longest narrow-answer key, and most keys one record may carry.
_NARROW_KEY = re.compile(r"[a-z][a-z0-9_]{0,47}")
_NARROW_MAX = 16
#: Labels `judge_questions.soft_band` writes.
SOFT_BANDS = frozenset({"low", "uncertain", "high"})


def narrow_fields(narrow: Mapping[str, object] | None, options: frozenset[str] | set[str] = frozenset()) -> dict:
    """The narrow answers a legacy answer shape was derived from, and their bands.

    Values are probabilities in [0, 1], or a choice drawn from ``options``; the
    keys are question names. The bands are logged labels and decide nothing.
    """
    if narrow is None:
        return {}
    if not isinstance(narrow, Mapping) or len(narrow) > _NARROW_MAX:
        _invalid()
    values: dict[str, object] = {}
    for key, value in narrow.items():
        if not isinstance(key, str) or not _NARROW_KEY.fullmatch(key):
            _invalid()
        values[key] = value if isinstance(value, str) and value in options else _number(value, 1)
    from ortus.core.judge_questions import soft_bands

    return {"narrow_answers": values, "soft_bands": soft_bands(values)}


def request_fields(latency_ms: float | None, usage: JudgeUsage | None) -> dict:
    """Latency and token counts for a site whose record did not carry them.

    All three are null when no request was made; a request that failed still
    has a latency and no usage.
    """
    counts = _usage(usage)
    return {
        "latency_ms": None if latency_ms is None else _number(latency_ms),
        "input_tokens": counts["input_tokens"],
        "output_tokens": counts["output_tokens"],
    }


def _clean_metadata(
    value: str, config: JudgeConfig, environ: Mapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    secrets = tuple(v for k, v in env.items() if re.search(
        r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", k, re.I,
    ) and v)

    try:
        return sanitize_field(
            value, cap=160, secret_values=secrets,
            sensitive_paths=config.sensitive_paths,
        ).value
    except StateError:
        raise JudgeLogError(LogFailure.INVALID_EVENT) from None


def _truncated_fields(value: object) -> list[str]:
    """The issue-text fields the packer shortened, as a bounded sorted list.

    Only the packer's own field names are accepted, so this stays a fixed
    vocabulary rather than one more place a caller could put free text.
    """
    if not isinstance(value, (list, tuple)) or len(value) > len(TRUNCATABLE_FIELDS) or any(
        not isinstance(name, str) for name in value
    ):
        _invalid()
    names = sorted(value)
    if len(set(names)) != len(names) or any(name not in TRUNCATABLE_FIELDS for name in names):
        _invalid()
    return names


GATE_ACTIONS = frozenset(action.value for action in GateAction)
#: What a pre_turn route pick may name, `none` included.
ROUTE_OPTIONS = frozenset({route.value for route in JudgeRoute} | {"none"})
#: `RouterTier` values, named here because importing the router module pulls in
#: every backend runner.
ROUTER_TIERS = frozenset({"cheap", "baseline", "frontier"})


def _criteria_version(version: str, digest: str) -> str | None:
    """The gate's question version, from the criteria fields it already logs."""
    candidate = f"{version}:{digest[:16]}"
    return candidate if QUESTION_VERSION.fullmatch(candidate) else None


def _decision_payload(
    event: DecisionEvent, config: JudgeConfig, environ: Mapping[str, str] | None,
) -> dict:
    if not isinstance(event, DecisionEvent) or not isinstance(event.decision, GateDecision):
        _invalid()

    def clean(value: str) -> str | None:
        return _clean_metadata(value, config, environ)

    payload = _common("decision", event.run_id, event.decision_id)
    if not isinstance(event.model, str) or not re.fullmatch(r"jev-\d+\.\d+\.\d+", event.model):
        _invalid()
    if not isinstance(event.criteria_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", event.criteria_hash):
        _invalid()
    if (event.answers is None) == (event.failure is None):
        _invalid()
    answers = None
    if event.answers is not None:
        a = event.answers
        if not isinstance(a, JudgeAnswers):
            _invalid()
        answers = {
            "route": _enum(a.route, JudgeRoute),
            "route_confidence": _number(a.route_confidence, 1),
            "needs_human": _number(a.needs_human, 1),
            "noul_confidence": _number(a.noul_confidence, 1),
            "action_risk": _number(a.action_risk, 2),
            "risk_confidence": _number(a.risk_confidence, 1),
        }
    action = _enum(event.decision.action, GateAction)
    backend = event.decision.backend
    if (action == "proceed") != (backend is not None) or (
        backend is not None and backend not in WORKER_ROUTES
    ):
        _invalid()
    payload.update({
        "seat": clean(event.seat),
        "issue_id": clean(event.issue_id),
        "phase": _enum(event.phase, JudgePhase),
        "answers": answers,
        "intended_action": answers["route"] if answers is not None else None,
        "effective_action": action,
        "backend": _enum(backend, JudgeRoute) if backend is not None else None,
        "reason": _enum(event.decision.reason, GateReason),
        "model": clean(event.model),
        "criteria_version": clean(event.criteria_version),
        "criteria_hash": clean(event.criteria_hash),
        "latency_ms": _number(event.latency_ms),
        "failure": _enum(event.failure, JudgeFailure) if event.failure is not None else None,
        "truncated_fields": _truncated_fields(event.truncated_fields),
        **_usage(event.usage),
        # Without the judge, grind claims the bead it selected; the gate's own
        # action is what Jev's answers argue for, and a failure argues nothing.
        **side_fields(
            GATE_ACTIONS, baseline_action=GateAction.PROCEED.value,
            jev_action=action if answers is not None else None,
            question_version=_criteria_version(event.criteria_version, event.criteria_hash),
            option_order=event.option_order or None, options=ROUTE_OPTIONS,
        ),
        **narrow_fields(event.answers.narrow if event.answers is not None else None,
                        ROUTE_OPTIONS),
    })
    if config.mode == JudgeMode.SHADOW:
        payload.update({
            "mode": "shadow",
            "observed_issue_id": clean(event.issue_id),
            "intended_action": action,
            "effective_action": "baseline",
        })
    return payload


def _append(repo: Path, payload: dict, *, name: str = LOG_NAME) -> None:
    try:
        data = (json.dumps(payload, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise JudgeLogError(LogFailure.INVALID_EVENT) from None
    if len(data) > MAX_EVENT_BYTES:
        _invalid()
    try:
        with ExitStack() as stack:
            def opened(path: str | Path, flags: int, *, parent: int | None = None) -> int:
                fd = os.open(path, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                stack.callback(os.close, fd)
                return fd

            root = opened(repo, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.mkdir("logs", mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            directory = opened("logs", os.O_RDONLY | os.O_DIRECTORY, parent=root)
            fd = opened(name, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK,
                        parent=directory)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise JudgeLogError(LogFailure.UNSAFE_PATH)
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Tighten permissive existing logs without broadening stricter modes.
            os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) & 0o600)
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                raise JudgeLogError(LogFailure.INCOMPLETE_LOG)
            try:
                if os.write(fd, data) != len(data):
                    raise OSError(errno.EIO, "short append")
                os.fsync(fd)
            except OSError:
                # Roll back a short/failed append while still holding the lock.
                os.ftruncate(fd, size)
                raise
    except OSError as exc:
        failure = LogFailure.UNSAFE_PATH if exc.errno in (errno.ELOOP, errno.ENOTDIR) else LogFailure.IO_ERROR
        raise JudgeLogError(failure) from None


def write_decision(
    repo: Path, config: JudgeConfig, event: DecisionEvent,
    *, environ: Mapping[str, str] | None = None,
) -> UUID | None:
    """Append before launch; return the correlation id, or None when disabled.

    Metadata is screened again even when it came from a packed judge state.
    Free-form state, provider prose and exception strings have no event fields.
    """
    if not config.enabled:
        return None
    payload = _decision_payload(event, config, environ)
    _append(repo, payload)
    output.progress("grind", f"judge {payload['effective_action']} "
                    f"reason={payload['reason']} failure={payload['failure'] or 'none'} "
                    f"latency_ms={payload['latency_ms']:.0f}")
    return event.decision_id


def _tool_vector(value: object) -> dict[str, float] | None:
    """A three-class pre_tool vector, or None for a local-policy record."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != TOOL_ACTIONS:
        _invalid()
    return {str(name): _number(weight, 1) for name, weight in value.items()}


def write_tool_decision(
    repo: Path, config: JudgeConfig, run_id: UUID, issue_id: str,
    record: Mapping[str, object],
) -> UUID:
    """Append one pre_tool outcome the hook published in its private inbox.

    The hook decides inside the worker's process, which owns the repository
    tree, so it leaves a typed record beside its park signals and the parent
    writes it here. Every decision is recorded, including the allowed ones and
    the ones local policy answered without a request: a phase whose refusals
    were previously visible only in a worker transcript is a phase nobody can
    tune. The record names the tool, its local reason or its vector, and what
    the hook applied — never a provider string. The one argument it may carry
    is `command`, which the hook redacted and this screens and caps again.
    """
    if not isinstance(record, Mapping):
        _invalid()
    for key in ("action", "effective_action"):
        if record.get(key) not in TOOL_ACTIONS:
            _invalid()
    for key in ("tool", "reason"):
        if not isinstance(record.get(key), str):
            _invalid()
    failure = record.get("failure")
    if failure is not None and failure not in {entry.value for entry in JudgeFailure}:
        _invalid()
    counts = (record.get("input_tokens"), record.get("output_tokens"))
    if any(count is not None for count in counts) and any(
        type(count) is not int or count < 0 for count in counts
    ):
        _invalid()
    usage = JudgeUsage(*counts) if all(type(c) is int for c in counts) else None
    command = record.get("command")
    if command is not None and not isinstance(command, str):
        _invalid()
    hook_failure = record.get("hook_failure")
    if hook_failure is not None and (
        not isinstance(hook_failure, str) or not HOOK_FAILURE.fullmatch(hook_failure)
    ):
        _invalid()
    payload = _common("tool_decision", run_id, uuid4())
    payload.update({
        "phase": "pre_tool", "mode": config.mode.value,
        "issue_id": _clean_metadata(issue_id, config),
        "seat": _clean_metadata(config.seat, config), "model": config.model,
        "tool": _clean_metadata(str(record["tool"]), config),
        "reason": _clean_metadata(str(record["reason"]), config),
        "vector": _tool_vector(record.get("vector")),
        "action": record["action"],
        "effective_action": record["effective_action"],
        "failure": failure,
        "latency_ms": _number(record.get("latency_ms", 0)),
        **_usage(usage),
    })
    if command is not None:
        # The hook already redacted and bounded it; screening the text again
        # here means a hook from another version cannot widen what is logged.
        payload["command"] = _screened_command(command, config)
    if hook_failure is not None:
        payload["hook_failure"] = hook_failure
    if any(key in record for key in SIDE_KEYS):
        payload.update(side_fields(
            TOOL_ACTIONS, baseline_action=record.get("baseline_action"),
            jev_action=record.get("jev_action"),
            question_version=record.get("question_version"),
            option_order=record.get("option_order"),
        ))
    payload.update(narrow_fields(record.get("narrow_answers")))
    _append(repo, payload)
    return UUID(payload["decision_id"])


#: A hook-side failure code: `invalid_input:<subreason>`, `oversize_input`,
#: `invalid_context`, `watchdog` or `internal`. Bounded so a record from
#: another hook version cannot smuggle text into the log through it.
HOOK_FAILURE = re.compile(r"[a-z_]{1,32}(?::[a-z_]{1,32})?")


#: The side fields a hook record may carry; a hook that predates them omits
#: all of them and its record is logged in the old shape.
SIDE_KEYS = ("baseline_action", "jev_action", "question_version", "option_order")


def _screened_command(command: str, config: JudgeConfig) -> str:
    secrets = environment_secrets()
    return cap_command(
        _screened(command, config, secrets, cap=len(command)) if command else "",
        min(config.tool_cap, MAX_COMMAND_CHARS),
    )


def write_model_route(
    repo: Path, config: JudgeConfig, event: ModelRouteEvent,
    *, environ: Mapping[str, str] | None = None,
) -> UUID | None:
    """Append one bead's routing record before its worker launches.

    Written for every routed bead, including the arms that changed nothing, so
    the A/B can divide cost by closed beads per arm instead of guessing which
    beads the router touched. A model name is operator-supplied configuration
    and is screened like any other metadata before it reaches the log.

    An observation carries ``applied=False`` and is the same record otherwise,
    so a shadow seat's evidence is readable by everything that already reads
    this log rather than needing a second format.
    """
    if not config.enabled:
        return None
    if not isinstance(event, ModelRouteEvent):
        _invalid()
    if type(event.applied) is not bool:
        _invalid()

    def clean(value: str | None) -> str | None:
        return _clean_metadata(value, config, environ) if value is not None else None

    payload = _common("model_route", event.run_id, event.decision_id)
    payload.update({
        "seat": clean(event.seat),
        "issue_id": clean(event.issue_id),
        "backend": clean(event.backend),
        "tier": clean(event.tier),
        "reason": clean(event.reason),
        "model": clean(event.model),
        "reasoning_effort": clean(event.reasoning_effort),
        "applied": event.applied,
        **{
            name: None if value is None else _number(value, 1)
            for name, value in (
                ("needs_frontier", event.needs_frontier),
                ("action_risk", event.action_risk),
                ("difficulty", event.difficulty),
            )
        },
        # The router's questions ride in the gate's request; their version is
        # named here so a reworded router question never scores against the old.
        **side_fields(
            ROUTER_TIERS, baseline_action="baseline", jev_action=event.jev_tier,
            question_version=event.question_version,
        ),
        **request_fields(None, None),
    })
    _append(repo, payload, name=ROUTE_LOG_NAME)
    output.progress("grind", f"judge route tier={payload['tier']} "
                    f"reason={payload['reason']} "
                    f"model={payload['model'] or 'provider-default'} "
                    f"effort={payload['reasoning_effort'] or 'provider-default'} "
                    f"applied={'yes' if event.applied else 'no'}")
    return event.decision_id


def write_outcome(repo: Path, config: JudgeConfig, event: OutcomeEvent) -> UUID | None:
    """Append only observed typed facts; this never closes or rejudges an issue."""
    if not config.enabled:
        return None
    if not isinstance(event, OutcomeEvent):
        _invalid()
    payload = _common("outcome", event.run_id, event.decision_id)
    payload.update({
        "observed_status": _enum(event.observed_status, OutcomeStatus),
        "elapsed_worker_ms": _number(event.elapsed_worker_ms),
        **_usage(event.usage),
    })
    _append(repo, payload)
    output.progress("grind", f"judge outcome {payload['observed_status']} "
                    f"elapsed_worker_ms={payload['elapsed_worker_ms']:.0f}")
    return event.decision_id


def write_shadow_outcome(
    repo: Path, config: JudgeConfig, event: OutcomeEvent,
    *, observed_issue_id: str, actual_claimed_id: str | None,
) -> UUID | None:
    """Keep unpaired observations out of accuracy data, without inventing outcomes."""
    if not config.enabled:
        return None
    if not isinstance(event, OutcomeEvent):
        _invalid()
    status = _enum(event.observed_status, OutcomeStatus)
    matched = actual_claimed_id is not None and actual_claimed_id == observed_issue_id
    payload = _common("outcome", event.run_id, event.decision_id)
    payload.update({
        "mode": "shadow",
        "observed_issue_id": _clean_metadata(observed_issue_id, config),
        "actual_claimed_id": (
            _clean_metadata(actual_claimed_id, config) if actual_claimed_id is not None else None
        ),
        "attribution_mismatch": actual_claimed_id is not None and not matched,
        "accuracy_eligible": matched and event.observed_status != OutcomeStatus.UNKNOWN,
        "observed_status": status if matched else None,
        "elapsed_worker_ms": _number(event.elapsed_worker_ms),
        **_usage(event.usage),
    })
    _append(repo, payload)
    return event.decision_id
