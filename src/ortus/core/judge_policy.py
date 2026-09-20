"""Pure pre-turn policy over typed answers, with no tracker or network effects."""

from __future__ import annotations

import math

from ortus.core.judge import (
    WORKER_ROUTES,
    FailureMode,
    GateAction,
    GateDecision,
    GateReason,
    JudgeAnswers,
    JudgeConfig,
    JudgeRoute,
    JudgeState,
)
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict, route_options
from ortus.core.profiles import ProfileError

_WORKERS = frozenset(WORKER_ROUTES)


def _valid_answers(answers: JudgeAnswers) -> bool:
    # Dataclass annotations do not enforce runtime values for direct callers.
    if type(answers) is not JudgeAnswers or not isinstance(answers.route, JudgeRoute):
        return False
    return all(
        type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)
        for value, maximum in (
            (answers.route_confidence, 1),
            (answers.noul_confidence, 1),
            (answers.risk_confidence, 1),
            (answers.needs_human, 1),
            (answers.action_risk, 2),
        )
    )


def decide_pre_turn(
    config: JudgeConfig,
    state: JudgeState,
    verdict: JudgeVerdict | None,
    *,
    baseline_backend: JudgeRoute,
    denied: bool = False,
    requires_human: bool = False,
) -> GateDecision:
    """Apply precedence without reading issue text or provider explanations.

    The caller resolves the baseline runner and hard constraints before asking
    the judge. Denial skips the turn; a human constraint pauses it. Neither can
    be overridden by a disabled judge or a fail-open response. This decision
    authorizes only worker routing, never an irreversible tool action.

    Enabled callers must have an available configured worker and an available
    baseline before any claim or provider request. Invalid setup raises rather
    than masquerading as a service outage. A missing enabled verdict is treated
    as an invalid answer.
    """
    if denied:
        return GateDecision(GateAction.SKIP, None, GateReason.POLICY_DENIED)
    if requires_human or "human" in state.labels:
        return GateDecision(GateAction.HUMAN, None, GateReason.POLICY_HUMAN)

    if baseline_backend not in _WORKERS or baseline_backend not in state.backends_available:
        raise ProfileError("judge baseline must be an available worker backend")
    if not config.enabled:
        return GateDecision(GateAction.PROCEED, baseline_backend, GateReason.DISABLED)

    offered = route_options(config, state)
    if not _WORKERS.intersection(offered):
        raise ProfileError("judge requires at least one available configured worker backend")

    answers = verdict.answers if verdict is not None else None
    failure = verdict.failure if verdict is not None else JudgeFailure.INVALID_ANSWER
    if failure is None and (
        answers is None or not _valid_answers(answers) or answers.route not in offered
    ):
        failure = JudgeFailure.INVALID_ANSWER
    if failure is not None:
        reason = (
            GateReason.INVALID_ANSWER
            if failure == JudgeFailure.INVALID_ANSWER
            else GateReason.SERVICE_FAILURE
        )
        if config.failure_mode == FailureMode.OPEN:
            return GateDecision(GateAction.PROCEED, baseline_backend, reason)
        return GateDecision(GateAction.HUMAN, None, reason)

    assert answers is not None
    if (
        answers.route_confidence < config.route_confidence
        or answers.noul_confidence < config.noul_confidence
        or answers.risk_confidence < config.risk_confidence
    ):
        return GateDecision(GateAction(config.low_confidence.value), None, GateReason.LOW_CONFIDENCE)
    if answers.needs_human >= config.human_threshold or answers.route == JudgeRoute.HUMAN:
        return GateDecision(GateAction.HUMAN, None, GateReason.NEEDS_HUMAN)
    if answers.action_risk >= config.risk_threshold:
        return GateDecision(GateAction.HUMAN, None, GateReason.HIGH_RISK)
    if answers.route == JudgeRoute.SKIP:
        return GateDecision(GateAction.SKIP, None, GateReason.ROUTED)
    return GateDecision(GateAction.PROCEED, answers.route, GateReason.ROUTED)
