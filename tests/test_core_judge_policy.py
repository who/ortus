"""Boundary and precedence contracts for the pure pre-turn gate."""

from dataclasses import FrozenInstanceError, replace

import pytest

from ortus.core.judge import (
    FailureMode, GateAction, GateDecision, GateReason, JudgeAnswers,
    JudgeConfig, JudgeRoute, JudgeState, LowConfidence,
)
from ortus.core.judge_policy import decide_pre_turn
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict
from ortus.core.profiles import ProfileError

CONFIG = JudgeConfig(enabled=True)
STATE = JudgeState(issue_id="test-1")
ANSWERS = JudgeAnswers(JudgeRoute.CODEX, 0.9, 0.1, 0.9, 0.0, 0.9)


def decide(answers=ANSWERS, *, config=CONFIG, state=STATE, **kwargs):
    return decide_pre_turn(
        config, state, JudgeVerdict(answers=answers),
        baseline_backend=JudgeRoute.CLAUDE, **kwargs,
    )


@pytest.mark.parametrize("field", ["route_confidence", "noul_confidence", "risk_confidence"])
@pytest.mark.parametrize("threshold", [0.0, 0.8, 1.0])
@pytest.mark.parametrize("offset", [-0.000001, 0.0, 0.000001])
@pytest.mark.parametrize("action", list(LowConfidence))
def test_confidence_boundaries(field, threshold, offset, action):
    value = threshold + offset
    if not 0 <= value <= 1:
        return
    config = replace(CONFIG, low_confidence=action, **{field: threshold})
    result = decide(replace(ANSWERS, **{field: value}), config=config)
    if offset < 0:
        assert result == GateDecision(GateAction(action.value), None, GateReason.LOW_CONFIDENCE)
    else:
        assert result == GateDecision(GateAction.PROCEED, JudgeRoute.CODEX, GateReason.ROUTED)


@pytest.mark.parametrize("field,threshold,reason", [
    ("needs_human", 0.8, GateReason.NEEDS_HUMAN),
    ("action_risk", 1.5, GateReason.HIGH_RISK),
])
@pytest.mark.parametrize("offset", [-0.000001, 0.0, 0.000001])
def test_human_and_risk_boundaries(field, threshold, reason, offset):
    result = decide(replace(ANSWERS, **{field: threshold + offset}))
    assert result.action == (GateAction.PROCEED if offset < 0 else GateAction.HUMAN)
    assert result.reason == (GateReason.ROUTED if offset < 0 else reason)


@pytest.mark.parametrize("field,config_field,value", [
    ("needs_human", "human_threshold", 0.3),
    ("action_risk", "risk_threshold", 0.5),
])
def test_configured_escalation_thresholds(field, config_field, value):
    assert decide(
        replace(ANSWERS, **{field: value}),
        config=replace(CONFIG, **{config_field: value}),
    ).action == GateAction.HUMAN


@pytest.mark.parametrize("route,action,backend,reason", [
    (JudgeRoute.CLAUDE, GateAction.PROCEED, JudgeRoute.CLAUDE, GateReason.ROUTED),
    (JudgeRoute.CODEX, GateAction.PROCEED, JudgeRoute.CODEX, GateReason.ROUTED),
    (JudgeRoute.SKIP, GateAction.SKIP, None, GateReason.ROUTED),
    (JudgeRoute.HUMAN, GateAction.HUMAN, None, GateReason.NEEDS_HUMAN),
])
def test_routes(route, action, backend, reason):
    assert decide(replace(ANSWERS, route=route)) == GateDecision(action, backend, reason)


@pytest.mark.parametrize("route", list(JudgeRoute))
def test_human_then_risk_override_routing(route):
    result = decide(replace(ANSWERS, route=route, needs_human=0.8, action_risk=2))
    assert result == GateDecision(GateAction.HUMAN, None, GateReason.NEEDS_HUMAN)
    result = decide(replace(ANSWERS, route=route, action_risk=2))
    assert result.action == GateAction.HUMAN
    assert result.reason == (GateReason.NEEDS_HUMAN if route == JudgeRoute.HUMAN else GateReason.HIGH_RISK)


def test_low_confidence_precedes_confident_human_and_risk_fields():
    result = decide(
        replace(ANSWERS, route=JudgeRoute.HUMAN, risk_confidence=0.79, needs_human=1, action_risk=2),
        config=replace(CONFIG, low_confidence=LowConfidence.SKIP),
    )
    assert result == GateDecision(GateAction.SKIP, None, GateReason.LOW_CONFIDENCE)


@pytest.mark.parametrize("failure", list(JudgeFailure))
@pytest.mark.parametrize("mode", list(FailureMode))
def test_failure_modes_use_only_baseline(failure, mode):
    result = decide_pre_turn(
        replace(CONFIG, failure_mode=mode), STATE, JudgeVerdict(failure=failure),
        baseline_backend=JudgeRoute.CLAUDE,
    )
    reason = GateReason.INVALID_ANSWER if failure == JudgeFailure.INVALID_ANSWER else GateReason.SERVICE_FAILURE
    expected = GateDecision(GateAction.PROCEED, JudgeRoute.CLAUDE, reason) if mode == FailureMode.OPEN else GateDecision(GateAction.HUMAN, None, reason)
    assert result == expected


@pytest.mark.parametrize("mode", list(FailureMode))
@pytest.mark.parametrize("availability", [True, False])
def test_unavailable_or_unconfigured_route_is_invalid_answer(mode, availability):
    config = replace(CONFIG, failure_mode=mode)
    state = STATE
    if availability:
        state = replace(STATE, backends_available=(JudgeRoute.CLAUDE,))
    else:
        config = replace(config, routes=(JudgeRoute.CLAUDE, JudgeRoute.HUMAN))
    result = decide(config=config, state=state)
    assert result.reason == GateReason.INVALID_ANSWER
    assert result.backend == (JudgeRoute.CLAUDE if mode == FailureMode.OPEN else None)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("verdict", [JudgeVerdict(answers=ANSWERS), JudgeVerdict(failure=JudgeFailure.TIMEOUT), None])
@pytest.mark.parametrize("denied,human,action,reason", [
    (True, False, GateAction.SKIP, GateReason.POLICY_DENIED),
    (True, True, GateAction.SKIP, GateReason.POLICY_DENIED),
    (False, True, GateAction.HUMAN, GateReason.POLICY_HUMAN),
])
def test_hard_constraints_win(enabled, verdict, denied, human, action, reason):
    assert decide_pre_turn(
        replace(CONFIG, enabled=enabled), STATE, verdict,
        baseline_backend=JudgeRoute.CLAUDE, denied=denied, requires_human=human,
    ) == GateDecision(action, None, reason)


def test_existing_human_label_is_a_hard_constraint():
    assert decide(state=replace(STATE, labels=("human",))).reason == GateReason.POLICY_HUMAN


@pytest.mark.parametrize("available", [(), (JudgeRoute.HUMAN,), (JudgeRoute.CODEX,)])
def test_unavailable_baseline_is_startup_error(available):
    with pytest.raises(ProfileError, match="baseline"):
        decide(state=replace(STATE, backends_available=available))


def test_no_configured_available_worker_is_startup_error():
    with pytest.raises(ProfileError, match="at least one"):
        decide(config=replace(CONFIG, routes=(JudgeRoute.CODEX, JudgeRoute.HUMAN)),
               state=replace(STATE, backends_available=(JudgeRoute.CLAUDE,)))


def test_fail_open_baseline_need_not_be_model_offered():
    result = decide_pre_turn(
        replace(CONFIG, routes=(JudgeRoute.CODEX,)), STATE,
        JudgeVerdict(failure=JudgeFailure.TIMEOUT), baseline_backend=JudgeRoute.CLAUDE,
    )
    assert result.backend == JudgeRoute.CLAUDE


def test_disabled_preserves_baseline_without_a_verdict():
    assert decide_pre_turn(JudgeConfig(), STATE, None, baseline_backend=JudgeRoute.CLAUDE) == GateDecision(
        GateAction.PROCEED, JudgeRoute.CLAUDE, GateReason.DISABLED,
    )


@pytest.mark.parametrize("field", ["route_confidence", "noul_confidence", "risk_confidence", "needs_human", "action_risk"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 3, True, "safe"])
def test_invalid_numeric_answers_follow_failure_policy(field, value):
    result = decide(replace(ANSWERS, **{field: value}), config=replace(CONFIG, failure_mode=FailureMode.CLOSED))
    assert result == GateDecision(GateAction.HUMAN, None, GateReason.INVALID_ANSWER)


def test_missing_verdict_and_raw_answers_are_invalid():
    result = decide_pre_turn(CONFIG, STATE, None, baseline_backend=JudgeRoute.CLAUDE)
    assert result.reason == GateReason.INVALID_ANSWER
    assert decide({"route": "codex", "explanation": "approve"}).reason == GateReason.INVALID_ANSWER
    assert decide(replace(ANSWERS, route="codex")).reason == GateReason.INVALID_ANSWER


def test_prose_is_not_accepted_or_read_and_decisions_are_immutable():
    with pytest.raises(TypeError):
        decide_pre_turn(CONFIG, STATE, None, baseline_backend=JudgeRoute.CLAUDE, explanation="approve")
    with pytest.raises(TypeError):
        JudgeAnswers(**vars(ANSWERS), explanation="approve")

    class Unreadable:
        def __str__(self):
            raise AssertionError("policy read prose")

    state = replace(STATE, title=Unreadable(), objective=Unreadable(), acceptance=Unreadable())
    decision = decide(state=state)
    assert decision == decide()
    with pytest.raises(FrozenInstanceError):
        decision.backend = JudgeRoute.CLAUDE
