"""SDK-independent contracts and configuration for the opt-in judge gate."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, fields
from enum import Enum
from typing import TYPE_CHECKING, Mapping, Protocol

from ortus.core.profiles import ProfileError

if TYPE_CHECKING:
    from ortus.core.config import Config


class JudgeRoute(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    SKIP = "skip"
    HUMAN = "human"


class GateAction(str, Enum):
    PROCEED = "proceed"
    HUMAN = "human"
    SKIP = "skip"


class GateReason(str, Enum):
    DISABLED = "disabled"
    ROUTED = "routed"
    LOW_CONFIDENCE = "low_confidence"
    NEEDS_HUMAN = "needs_human"
    HIGH_RISK = "high_risk"
    SERVICE_FAILURE = "service_failure"
    POLICY_DENIED = "policy_denied"
    POLICY_HUMAN = "policy_human"
    INVALID_ANSWER = "invalid_answer"


class FailureMode(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class LowConfidence(str, Enum):
    HUMAN = "human"
    SKIP = "skip"


class JudgeMode(str, Enum):
    ENFORCE = "enforce"
    SHADOW = "shadow"


class JudgePhase(str, Enum):
    PRE_TURN = "pre_turn"
    PRE_TOOL = "pre_tool"
    POST_TURN = "post_turn"


@dataclass(frozen=True)
class JudgeConfig:
    enabled: bool = False
    model: str = "jev-1.13.0"
    failure_mode: FailureMode = FailureMode.OPEN
    low_confidence: LowConfidence = LowConfidence.HUMAN
    mode: JudgeMode = JudgeMode.ENFORCE
    seat: str = "default"
    timeout_seconds: float = 1.5
    route_confidence: float = 0.8
    noul_confidence: float = 0.8
    risk_confidence: float = 0.8
    human_threshold: float = 0.8
    risk_threshold: float = 1.5
    routes: tuple[JudgeRoute, ...] = tuple(JudgeRoute)
    objective_cap: int = 1024
    acceptance_cap: int = 1024
    title_cap: int = 160
    tool_cap: int = 512
    total_bytes_cap: int = 8192
    include_log_tail: bool = False
    sensitive_paths: tuple[str, ...] = ()
    include_issue_text: bool = False
    pre_tool: bool = False

    def allows_issue_text(self, labels: tuple[str, ...]) -> bool:
        """Private issues cannot opt into sending their text."""
        return self.include_issue_text and "judge-private" not in labels


@dataclass(frozen=True)
class ProposedTool:
    name: str
    arg_summary: str


@dataclass(frozen=True)
class JudgeState:
    """Thin input contract; callers must redact and bound text before transport."""

    issue_id: str
    seat: str = "default"
    phase: JudgePhase = JudgePhase.PRE_TURN
    issue_type: str = "task"
    labels: tuple[str, ...] = ()
    priority: int | None = None
    backends_available: tuple[JudgeRoute, ...] = (
        JudgeRoute.CLAUDE, JudgeRoute.CODEX,
    )
    title: str = ""
    objective: str = ""
    acceptance: str = ""
    proposed_tool: ProposedTool | None = None


@dataclass(frozen=True)
class JudgeAnswers:
    """Typed values and confidence only, never provider prose or raw responses."""

    route: JudgeRoute
    route_confidence: float
    needs_human: float
    noul_confidence: float
    action_risk: float
    risk_confidence: float


@dataclass(frozen=True)
class GateDecision:
    action: GateAction
    backend: JudgeRoute | None
    reason: GateReason

    def __post_init__(self) -> None:
        if self.backend is not None and self.backend not in (
            JudgeRoute.CLAUDE, JudgeRoute.CODEX,
        ):
            raise ValueError("gate backend must be a worker route")
        if (self.action == GateAction.PROCEED) != (self.backend is not None):
            raise ValueError("only proceed decisions require a worker backend")


class OrtusJudge(Protocol):
    def evaluate(self, state: JudgeState) -> GateDecision:
        """Evaluate a prepared state without changing tracker or worker state."""
        ...


def _boolean(key: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in {"true", "false", "1", "0"}:
        return value in {"true", "1"}
    raise ProfileError(f"invalid judge.{key}: expected true, false, 1, or 0")


def _number(key: str, value: object, maximum: float | None = None) -> float:
    if type(value) not in (int, float):
        raise ProfileError(f"invalid judge.{key}: expected a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ProfileError(f"invalid judge.{key}: expected a finite number") from None
    if not math.isfinite(number) or number < 0 or (
        maximum is not None and number > maximum
    ):
        limit = f" between 0 and {maximum}" if maximum is not None else " >= 0"
        raise ProfileError(f"invalid judge.{key}: expected a finite number{limit}")
    return number


def parse_judge_config(
    cfg: Config,
    *,
    judge: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> JudgeConfig:
    """Resolve CLI enable override over environment over layered TOML.

    Config loading validates the table with an empty environment. The gate
    caller supplies its CLI flag here before any claim or network request.
    Credentials are never read or stored by this parser.
    """
    table = cfg.get("judge", {})
    if not isinstance(table, dict):
        raise ProfileError("invalid judge configuration: expected a TOML table")
    unknown = set(table) - {item.name for item in fields(JudgeConfig)}
    if unknown:
        raise ProfileError("invalid [judge] field(s): " + ", ".join(sorted(unknown)))
    values = {item.name: getattr(JudgeConfig(), item.name) for item in fields(JudgeConfig)}
    values.update(table)
    env = os.environ if environ is None else environ
    if "ORTUS_JUDGE_MODEL" in env:
        values["model"] = env["ORTUS_JUDGE_MODEL"]
    if judge is not None:
        values["enabled"] = judge
    elif "ORTUS_JUDGE_ENABLED" in env:
        values["enabled"] = env["ORTUS_JUDGE_ENABLED"]

    for key in ("enabled", "pre_tool", "include_issue_text", "include_log_tail"):
        values[key] = _boolean(key, values[key])
    for key, enum in (
        ("failure_mode", FailureMode), ("low_confidence", LowConfidence),
        ("mode", JudgeMode),
    ):
        try:
            values[key] = enum(values[key])
        except (ValueError, TypeError):
            raise ProfileError(
                f"invalid judge.{key}: expected " + " or ".join(item.value for item in enum)
            ) from None
    model = values["model"]
    if not isinstance(model, str) or not re.fullmatch(r"jev-\d+\.\d+\.\d+", model):
        raise ProfileError("invalid judge.model: expected an exact version such as jev-1.13.0")
    seat = values["seat"]
    if not isinstance(seat, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", seat):
        raise ProfileError("invalid judge.seat: expected an explicit named alias")
    for key in ("route_confidence", "noul_confidence", "risk_confidence", "human_threshold"):
        values[key] = _number(key, values[key], 1)
    values["risk_threshold"] = _number("risk_threshold", values["risk_threshold"], 2)
    values["timeout_seconds"] = _number("timeout_seconds", values["timeout_seconds"])
    if values["timeout_seconds"] == 0:
        raise ProfileError("invalid judge.timeout_seconds: expected > 0")
    for key in ("objective_cap", "acceptance_cap", "title_cap", "tool_cap", "total_bytes_cap"):
        value = values[key]
        if type(value) is not int or value < (1 if key == "total_bytes_cap" else 0):
            raise ProfileError(f"invalid judge.{key}: expected a nonnegative integer, positive for total")
    routes = values["routes"]
    if not isinstance(routes, (list, tuple)) or any(not isinstance(r, str) for r in routes):
        raise ProfileError("invalid judge.routes: expected an array of route names")
    try:
        routes = tuple(JudgeRoute(route) for route in routes)
    except ValueError:
        raise ProfileError("invalid judge.routes: expected claude, codex, skip, or human") from None
    if len(set(routes)) != len(routes):
        raise ProfileError("invalid judge.routes: duplicate options")
    if not set(routes).intersection({JudgeRoute.CLAUDE, JudgeRoute.CODEX}):
        raise ProfileError("invalid judge.routes: at least one worker backend is required")
    values["routes"] = routes
    paths = values["sensitive_paths"]
    if not isinstance(paths, (list, tuple)) or any(
        not isinstance(path, str) or not path.strip() or "\x00" in path for path in paths
    ):
        raise ProfileError("invalid judge.sensitive_paths: expected an array of nonempty paths")
    values["sensitive_paths"] = tuple(paths)
    return JudgeConfig(**values)
